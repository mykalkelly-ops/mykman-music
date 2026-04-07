from pathlib import Path

from fastapi import FastAPI, Depends, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .db import engine, get_session
from .models import Artist, Album, Song, Playlist, PlaylistSong, init_db

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
