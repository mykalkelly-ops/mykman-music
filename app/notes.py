"""Helpers for notes: resolving targets, rendering, etc."""
import markdown as md
from sqlalchemy.orm import Session

from .models import Song, Album, Artist, Note


def render_markdown(text: str) -> str:
    return md.markdown(text or "", extensions=["fenced_code", "tables", "nl2br"])


def resolve_target(db: Session, target_type: str, target_id: int | None) -> dict:
    """Return a display-friendly dict describing what a note is attached to."""
    if target_type == "general" or target_id is None:
        return {"type": "general", "label": "General update", "sublabel": ""}
    if target_type == "song":
        s = db.get(Song, target_id)
        if not s:
            return {"type": "song", "label": f"[deleted song {target_id}]", "sublabel": ""}
        return {
            "type": "song",
            "label": s.title,
            "sublabel": f"{s.album.artist.name} · {s.album.title}" if s.album else "",
        }
    if target_type == "album":
        a = db.get(Album, target_id)
        if not a:
            return {"type": "album", "label": f"[deleted album {target_id}]", "sublabel": ""}
        return {"type": "album", "label": a.title, "sublabel": a.artist.name if a.artist else ""}
    if target_type == "artist":
        ar = db.get(Artist, target_id)
        if not ar:
            return {"type": "artist", "label": f"[deleted artist {target_id}]", "sublabel": ""}
        return {"type": "artist", "label": ar.name, "sublabel": ""}
    return {"type": target_type, "label": "?", "sublabel": ""}


def search_targets(db: Session, q: str, limit: int = 8) -> list[dict]:
    """Search songs, albums, and artists by name for target pickers."""
    like = f"%{q}%"
    results: list[dict] = []
    for ar in db.query(Artist).filter(Artist.name.ilike(like)).limit(limit).all():
        results.append({"type": "artist", "id": ar.id, "label": ar.name, "sublabel": ""})
    for al in db.query(Album).join(Artist).filter(Album.title.ilike(like)).limit(limit).all():
        results.append({"type": "album", "id": al.id, "label": al.title, "sublabel": al.artist.name})
    for s in db.query(Song).join(Album).join(Artist).filter(Song.title.ilike(like)).limit(limit).all():
        results.append({"type": "song", "id": s.id, "label": s.title, "sublabel": f"{s.album.artist.name} · {s.album.title}"})
    return results[: limit * 2]
