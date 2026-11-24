"""Explicit listening progress and queue; never rewrites ranking records."""
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import is_admin, require_admin
from .db import get_session
from .models import ListeningTrack, ListeningProgress

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def target_key(kind, *parts):
    return json.dumps([kind, *parts], ensure_ascii=False, separators=(",", ":"))


class InventoryTrack(BaseModel):
    library_id: str = Field(min_length=1, max_length=300)
    catalog_id: str | None = Field(default=None, max_length=300)
    artist: str = Field(min_length=1, max_length=1000)
    album: str = Field(min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=1000)
    disc: int | None = Field(default=None, ge=0)
    track: int | None = Field(default=None, ge=0)


class InventoryBody(BaseModel):
    tracks: list[InventoryTrack] = Field(max_length=100000)
    recent_catalog_ids: list[str] = Field(default_factory=list, max_length=10000)
    recent_library_ids: list[str] = Field(default_factory=list, max_length=10000)


def save_inventory(db, body):
    existing = {t.library_id: t for t in db.query(ListeningTrack).all()}
    recent_catalog = set(body.recent_catalog_ids)
    recent_library = set(body.recent_library_ids)
    now = datetime.utcnow()
    seen = set()
    for item in body.tracks:
        if item.library_id in seen:
            continue
        seen.add(item.library_id)
        row = existing.get(item.library_id)
        if row is None:
            row = ListeningTrack(library_id=item.library_id)
            db.add(row)
        for name in ("catalog_id", "artist", "album", "title", "disc", "track"):
            setattr(row, name, getattr(item, name))
        row.seen_at = now
        if item.library_id in recent_library or (item.catalog_id and item.catalog_id in recent_catalog):
            row.recent_evidence_at = now
    # Missing tracks are retained. Progress and rankings are never inferred or rewritten.
    db.commit()
    return {"ok": True, "tracks": len(seen)}


def inventory_view(db):
    tracks = db.query(ListeningTrack).order_by(ListeningTrack.artist, ListeningTrack.album, ListeningTrack.disc, ListeningTrack.track, ListeningTrack.title).all()
    progress = {p.target_key: p for p in db.query(ListeningProgress).all()}
    groups = {}
    for track in tracks:
        ak = target_key("artist", track.artist)
        bk = target_key("album", track.artist, track.album)
        sk = target_key("song", track.library_id)
        artist = groups.setdefault(ak, {"key": ak, "name": track.artist, "albums": {}})
        album = artist["albums"].setdefault(bk, {"key": bk, "title": track.album, "tracks": []})
        candidates = [progress[k] for k in (ak, bk, sk) if k in progress
            and progress[k].updated_at is not None
            and (k == sk or progress[k].status != "in_progress")
            and (k == sk or progress[k].updated_at >= track.first_seen_at)]
        decision = max(candidates, key=lambda p: p.updated_at) if candidates else None
        album["tracks"].append({"key": sk, "id": track.library_id, "title": track.title,
            "track": track.track, "disc": track.disc,
            "status": decision.status if decision else "unknown",
            "recent": bool(track.recent_evidence_at)})
    targets = {}
    for artist in groups.values():
        artist["albums"] = list(artist["albums"].values())
        targets[artist["key"]] = {"label": artist["name"], "artist_key": artist["key"]}
        for album in artist["albums"]:
            targets[album["key"]] = {"label": f'{artist["name"]} — {album["title"]}', "artist_key": artist["key"]}
            for track in album["tracks"]:
                targets[track["key"]] = {"label": f'{artist["name"]} — {album["title"]} — {track["title"]}', "artist_key": artist["key"]}
            album["heard"] = sum(t["status"] == "complete" for t in album["tracks"])
            album["status"] = progress[album["key"]].status if album["key"] in progress else "unknown"
        artist["status"] = progress[artist["key"]].status if artist["key"] in progress else "unknown"
        artist["heard"] = sum(a["heard"] for a in artist["albums"])
        artist["total"] = sum(len(a["tracks"]) for a in artist["albums"])
        artist["needs_review"] = artist["status"] == "complete" and artist["heard"] < artist["total"]
    queue = []
    for p in sorted(progress.values(), key=lambda p: p.queued_at or datetime.min, reverse=True):
        if p.queued_at and p.target_key in targets:
            queue.append({"key": p.target_key, "type": p.target_type, "status": p.status,
                "note": p.note or "", **targets[p.target_key]})
    next_track = None
    if queue:
        top_key = queue[0]["key"]
        candidates = []
        for artist in groups.values():
            for album in artist["albums"]:
                for track in album["tracks"]:
                    if top_key in (artist["key"], album["key"], track["key"]) and track["status"] != "complete":
                        candidates.append({**track, "artist": artist["name"], "album": album["title"]})
        if candidates:
            next_track = next((t for t in candidates if t["status"] == "in_progress"), candidates[0])
    return {"artists": list(groups.values()), "queue": queue, "targets": targets,
        "track_count": len(tracks), "next_track": next_track}


class ProgressBody(BaseModel):
    key: str = Field(max_length=2500)
    status: Literal["unknown", "unheard", "in_progress", "complete"] | None = None
    queue: Literal["top", "remove"] | None = None
    note: str | None = Field(default=None, max_length=2000)


def update_progress(db, body):
    view = inventory_view(db)
    if body.key not in view["targets"]:
        raise HTTPException(404, "Listening target not found; scan and save your library first.")
    row = db.get(ListeningProgress, body.key)
    if row is None:
        row = ListeningProgress(target_key=body.key, target_type=json.loads(body.key)[0], status="unknown")
        db.add(row)
    if body.status is not None:
        row.status = body.status
        row.updated_at = datetime.utcnow()
        if body.status == "in_progress":
            row.queued_at = datetime.utcnow()
        if body.status == "complete":
            # Finish this target and clear queued descendants covered by it.
            parts = json.loads(body.key)
            descendants = {body.key}
            for artist in view["artists"]:
                if parts[0] == "artist" and artist["key"] == body.key:
                    descendants.update(a["key"] for a in artist["albums"])
                    descendants.update(t["key"] for a in artist["albums"] for t in a["tracks"])
                for album in artist["albums"]:
                    if parts[0] == "album" and album["key"] == body.key:
                        descendants.update(t["key"] for t in album["tracks"])
            for queued in db.query(ListeningProgress).filter(ListeningProgress.queued_at.isnot(None)).all():
                if queued.target_key in descendants:
                    queued.queued_at = None
            row.queued_at = None
    if body.queue is not None:
        row.queued_at = datetime.utcnow() if body.queue == "top" else None
    if body.note is not None:
        row.note = body.note
    db.commit()
    return {"ok": True}


@router.get("/listening", response_class=HTMLResponse)
def listening_page(request: Request, q: str = "", db: Session = Depends(get_session)):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    view = inventory_view(db)
    # Show one artist at a time; the queue remains visible across searches.
    matching = [a for a in view["artists"] if q.casefold() in a["name"].casefold()]
    selected = next((a for a in matching if a["name"].casefold() == q.casefold()), matching[0] if matching and q else None)
    return templates.TemplateResponse(request, "listening.html", {**view,
        "matches": matching[:100], "selected": selected, "q": q})


@router.post("/api/listening/inventory")
def import_inventory(body: InventoryBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    return save_inventory(db, body)


@router.post("/api/listening/progress")
def change_progress(body: ProgressBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    return update_progress(db, body)


@router.get("/api/listening/export")
def export_progress(request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    def serialize(row):
        return {c.name: (getattr(row, c.name).isoformat() if isinstance(getattr(row, c.name), datetime) else getattr(row, c.name)) for c in row.__table__.columns}
    return JSONResponse({"version": 1, "exported_at": datetime.utcnow().isoformat(),
        "tracks": [serialize(t) for t in db.query(ListeningTrack).all()],
        "progress": [serialize(p) for p in db.query(ListeningProgress).all()]},
        headers={"Content-Disposition": 'attachment; filename="mykman-listening-progress.json"'})
