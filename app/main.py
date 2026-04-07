from pathlib import Path

from fastapi import FastAPI, Depends, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .db import engine, get_session
from .models import Artist, Album, Song, Playlist, PlaylistSong, Comparison, Note, init_db
from .glicko import update_pair
from .pair_selector import pick_pair
from .placement import update_bounds, maybe_finalize
from .scoring import album_scores, artist_scores, star_tier
from .notes import render_markdown, resolve_target, search_targets

app = FastAPI(title="Music Ranker")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.on_event("startup")
def on_startup():
    init_db(engine)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_session)):
    total_songs = db.query(func.count(Song.id)).scalar() or 0
    total_artists = db.query(func.count(Artist.id)).scalar() or 0
    total_albums = db.query(func.count(Album.id)).scalar() or 0
    total_playlists = db.query(func.count(Playlist.id)).scalar() or 0
    total_playlist_songs = db.query(func.count(PlaylistSong.id)).scalar() or 0

    playlists = (
        db.query(Playlist)
        .order_by(Playlist.year.desc().nullslast(), Playlist.month.desc().nullslast())
        .all()
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "total_songs": total_songs,
            "total_artists": total_artists,
            "total_albums": total_albums,
            "total_playlists": total_playlists,
            "total_playlist_songs": total_playlist_songs,
            "playlists": playlists,
        },
    )


@app.get("/playlists/{playlist_id}", response_class=HTMLResponse)
def playlist_detail(playlist_id: int, request: Request, db: Session = Depends(get_session)):
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        return HTMLResponse("Playlist not found", status_code=404)

    rows = (
        db.query(PlaylistSong)
        .options(joinedload(PlaylistSong.song).joinedload(Song.album).joinedload(Album.artist))
        .filter(PlaylistSong.playlist_id == playlist_id)
        .all()
    )
    return templates.TemplateResponse(
        request, "playlist.html", {"playlist": playlist, "rows": rows}
    )


@app.get("/songs", response_class=HTMLResponse)
def songs_list(
    request: Request,
    q: str | None = Query(None),
    limit: int = 200,
    db: Session = Depends(get_session),
):
    query = (
        db.query(Song)
        .options(joinedload(Song.album).joinedload(Album.artist))
    )
    if q:
        like = f"%{q}%"
        query = (
            query.join(Song.album).join(Album.artist)
            .filter((Song.title.ilike(like)) | (Album.title.ilike(like)) | (Artist.name.ilike(like)))
        )
    songs = query.order_by(Song.glicko_rating.desc()).limit(limit).all()
    songs_with_stars = [(s, star_tier(s.glicko_rating, s.glicko_rd)) for s in songs]
    return templates.TemplateResponse(
        request, "songs.html",
        {"songs_with_stars": songs_with_stars, "q": q or "", "limit": limit},
    )


@app.get("/albums", response_class=HTMLResponse)
def albums_page(request: Request, db: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request, "albums.html", {"albums": album_scores(db)}
    )


@app.get("/artists", response_class=HTMLResponse)
def artists_page(request: Request, db: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request, "artists.html", {"artists": artist_scores(db)}
    )


# ---------- Gender / band prompt queue ----------

@app.get("/api/next-artist-prompt")
def next_artist_prompt(db: Session = Depends(get_session)):
    """Return one artist that still needs gender/band metadata, prioritizing
    artists with songs in playlists."""
    artist = (
        db.query(Artist)
        .join(Album, Album.artist_id == Artist.id)
        .join(Song, Song.album_id == Album.id)
        .join(PlaylistSong, PlaylistSong.song_id == Song.id)
        .filter(Artist.gender.is_(None))
        .distinct()
        .first()
    )
    if artist is None:
        # fall back to any artist without gender set
        artist = db.query(Artist).filter(Artist.gender.is_(None)).first()
    if artist is None:
        return {"artist": None}
    return {"artist": {"id": artist.id, "name": artist.name}}


class ArtistMetaBody(BaseModel):
    artist_id: int
    gender: str  # M, F, NB, Band, Unknown


@app.post("/api/artist-meta")
def set_artist_meta(body: ArtistMetaBody, db: Session = Depends(get_session)):
    artist = db.get(Artist, body.artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    if body.gender not in ("M", "F", "NB", "Band", "Unknown"):
        raise HTTPException(400, "invalid gender value")
    artist.gender = body.gender
    artist.is_band = body.gender == "Band"
    db.commit()
    return {"ok": True}


# ---------- Notes / Blog ----------

@app.get("/notes", response_class=HTMLResponse)
def notes_index(request: Request, db: Session = Depends(get_session)):
    notes = db.query(Note).order_by(Note.created_at.desc()).all()
    items = []
    for n in notes:
        items.append({
            "id": n.id,
            "title": n.title or "",
            "body_html": render_markdown(n.body),
            "created_at": n.created_at,
            "updated_at": n.updated_at,
            "target": resolve_target(db, n.target_type, n.target_id),
        })
    return templates.TemplateResponse(request, "notes_index.html", {"items": items})


@app.get("/notes/new", response_class=HTMLResponse)
def notes_new(
    request: Request,
    target_type: str = "general",
    target_id: int | None = None,
    db: Session = Depends(get_session),
):
    target = resolve_target(db, target_type, target_id) if target_type != "general" else None
    return templates.TemplateResponse(
        request, "notes_edit.html",
        {"note": None, "target_type": target_type, "target_id": target_id, "target": target},
    )


@app.get("/notes/{note_id}/edit", response_class=HTMLResponse)
def notes_edit(note_id: int, request: Request, db: Session = Depends(get_session)):
    n = db.get(Note, note_id)
    if n is None:
        return HTMLResponse("Not found", status_code=404)
    target = resolve_target(db, n.target_type, n.target_id)
    return templates.TemplateResponse(
        request, "notes_edit.html",
        {"note": n, "target_type": n.target_type, "target_id": n.target_id, "target": target},
    )


class NoteBody(BaseModel):
    target_type: str = "general"
    target_id: int | None = None
    title: str | None = None
    body: str = ""


@app.post("/api/notes")
def create_note(body: NoteBody, db: Session = Depends(get_session)):
    if body.target_type not in ("song", "album", "artist", "general"):
        raise HTTPException(400, "invalid target_type")
    n = Note(
        target_type=body.target_type,
        target_id=body.target_id if body.target_type != "general" else None,
        title=body.title,
        body=body.body,
    )
    db.add(n)
    db.commit()
    return {"id": n.id}


@app.put("/api/notes/{note_id}")
def update_note(note_id: int, body: NoteBody, db: Session = Depends(get_session)):
    n = db.get(Note, note_id)
    if n is None:
        raise HTTPException(404, "note not found")
    n.title = body.title
    n.body = body.body
    db.commit()
    return {"ok": True}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int, db: Session = Depends(get_session)):
    n = db.get(Note, note_id)
    if n is None:
        raise HTTPException(404, "note not found")
    db.delete(n)
    db.commit()
    return {"ok": True}


@app.get("/api/target-search")
def api_target_search(q: str, db: Session = Depends(get_session)):
    if not q or len(q) < 2:
        return {"results": []}
    return {"results": search_targets(db, q)}


# ---------- Stats / Analytics ----------

@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, db: Session = Depends(get_session)):
    liked_song_ids = {sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()}

    def bucket(key_fn, songs):
        counts: dict = {}
        for s in songs:
            k = key_fn(s) or "Unknown"
            counts[k] = counts.get(k, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])

    liked_songs = (
        db.query(Song).join(Album).join(Artist)
        .filter(Song.id.in_(liked_song_ids)).all()
    ) if liked_song_ids else []

    by_genre = bucket(lambda s: s.album.genre, liked_songs)
    by_decade = bucket(
        lambda s: f"{(s.album.year // 10) * 10}s" if s.album and s.album.year else None,
        liked_songs,
    )
    by_gender = bucket(lambda s: s.album.artist.gender if s.album and s.album.artist else None, liked_songs)

    total_songs_in_lib = db.query(func.count(Song.id)).scalar() or 0
    total_liked = len(liked_song_ids)
    total_comparisons = db.query(func.count(Comparison.id)).scalar() or 0

    # Best monthly playlists by average rating of included songs
    playlist_rows = []
    for p in db.query(Playlist).all():
        avg = (
            db.query(func.avg(Song.glicko_rating))
            .join(PlaylistSong, PlaylistSong.song_id == Song.id)
            .filter(PlaylistSong.playlist_id == p.id)
            .scalar()
        )
        if avg is not None:
            playlist_rows.append((p.name, p.year, p.month, float(avg)))
    playlist_rows.sort(key=lambda r: -r[3])

    return templates.TemplateResponse(
        request, "stats.html",
        {
            "total_songs_in_lib": total_songs_in_lib,
            "total_liked": total_liked,
            "total_comparisons": total_comparisons,
            "by_genre": by_genre,
            "by_decade": by_decade,
            "by_gender": by_gender,
            "playlist_rows": playlist_rows,
        },
    )


# ---------- Comparisons ----------

def _song_payload(s: Song) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "album": s.album.title if s.album else None,
        "artist": s.album.artist.name if s.album and s.album.artist else None,
        "year": s.album.year if s.album else None,
        "genre": s.album.genre if s.album else None,
        "rating": round(s.glicko_rating, 1),
        "rd": round(s.glicko_rd, 1),
        "comparison_count": s.comparison_count,
    }


@app.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request):
    return templates.TemplateResponse(request, "compare.html", {})


@app.get("/api/next-pair")
def next_pair(db: Session = Depends(get_session)):
    pair = pick_pair(db)
    if pair is None:
        return JSONResponse({"error": "not enough songs"}, status_code=404)
    a, b = pair
    total_comparisons = db.query(func.count(Comparison.id)).scalar() or 0
    return {
        "a": _song_payload(a),
        "b": _song_payload(b),
        "total_comparisons": total_comparisons,
    }


class CompareBody(BaseModel):
    song_a_id: int
    song_b_id: int
    winner_id: int | None  # null = skip/tie


@app.post("/api/compare")
def submit_comparison(body: CompareBody, db: Session = Depends(get_session)):
    a = db.get(Song, body.song_a_id)
    b = db.get(Song, body.song_b_id)
    if a is None or b is None:
        raise HTTPException(404, "song not found")
    if body.winner_id not in (a.id, b.id, None):
        raise HTTPException(400, "winner must be one of the two songs or null")

    if body.winner_id is None:
        score_a = 0.5
    elif body.winner_id == a.id:
        score_a = 1.0
    else:
        score_a = 0.0

    (a_rating, a_rd, a_vol), (b_rating, b_rd, b_vol) = update_pair(
        a.glicko_rating, a.glicko_rd, a.glicko_vol,
        b.glicko_rating, b.glicko_rd, b.glicko_vol,
        score_a,
    )
    a.glicko_rating, a.glicko_rd, a.glicko_vol = a_rating, a_rd, a_vol
    b.glicko_rating, b.glicko_rd, b.glicko_vol = b_rating, b_rd, b_vol
    a.comparison_count = (a.comparison_count or 0) + 1
    b.comparison_count = (b.comparison_count or 0) + 1

    # Update binary-search placement bounds for any pending songs.
    # A tie/skip does not update bounds.
    if body.winner_id is not None:
        a_won = body.winner_id == a.id
        if a.placement_pending:
            update_bounds(a, b, a_won)
            maybe_finalize(a)
        if b.placement_pending:
            update_bounds(b, a, not a_won)
            maybe_finalize(b)

    db.add(Comparison(song_a_id=a.id, song_b_id=b.id, winner_id=body.winner_id))
    db.commit()

    return {
        "a": _song_payload(a),
        "b": _song_payload(b),
    }
