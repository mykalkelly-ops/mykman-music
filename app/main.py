from pathlib import Path

from fastapi import FastAPI, Depends, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .db import engine, get_session
from .models import Artist, Album, Song, Playlist, PlaylistSong, Comparison, init_db
from .glicko import update_pair
from .pair_selector import pick_pair

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
        "index.html",
        {
            "request": request,
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
        "playlist.html",
        {"request": request, "playlist": playlist, "rows": rows},
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
    return templates.TemplateResponse(
        "songs.html",
        {"request": request, "songs": songs, "q": q or "", "limit": limit},
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
    return templates.TemplateResponse("compare.html", {"request": request})


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

    db.add(Comparison(song_a_id=a.id, song_b_id=b.id, winner_id=body.winner_id))
    db.commit()

    return {
        "a": _song_payload(a),
        "b": _song_payload(b),
    }
