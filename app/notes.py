"""Helpers for notes: resolving targets, rendering, etc."""
import markdown as md
from sqlalchemy.orm import Session

from .models import Song, Album, Artist, Note, NoteSong


def render_markdown(text: str) -> str:
    return md.markdown(text or "", extensions=["fenced_code", "tables", "nl2br"])


def resolve_target(db: Session, target_type: str, target_id: int | None) -> dict:
    """Return a display-friendly dict describing what a note is attached to."""
    if target_type == "general" or target_id is None:
        return {"type": "general", "label": "General update", "sublabel": "", "url": None}
    if target_type == "song":
        song = db.get(Song, target_id)
        if not song:
            return {"type": "song", "label": f"[deleted song {target_id}]", "sublabel": "", "url": None}
        return {
            "type": "song",
            "label": song.title,
            "sublabel": f"{song.album.artist.name} · {song.album.title}" if song.album else "",
            "url": f"/songs/{song.id}",
        }
    if target_type == "album":
        album = db.get(Album, target_id)
        if not album:
            return {"type": "album", "label": f"[deleted album {target_id}]", "sublabel": "", "url": None}
        return {
            "type": "album",
            "label": album.title,
            "sublabel": album.artist.name if album.artist else "",
            "url": f"/albums/{album.id}",
        }
    if target_type == "artist":
        artist = db.get(Artist, target_id)
        if not artist:
            return {"type": "artist", "label": f"[deleted artist {target_id}]", "sublabel": "", "url": None}
        return {"type": "artist", "label": artist.name, "sublabel": "", "url": f"/artists/{artist.id}"}
    return {"type": target_type, "label": "?", "sublabel": "", "url": None}


def search_targets(db: Session, q: str, limit: int = 8) -> list[dict]:
    """Search songs, albums, and artists by name for target pickers."""
    like = f"%{q}%"
    results: list[dict] = []
    for artist in db.query(Artist).filter(Artist.name.ilike(like)).limit(limit).all():
        results.append({"type": "artist", "id": artist.id, "label": artist.name, "sublabel": ""})
    for album in db.query(Album).join(Artist).filter(Album.title.ilike(like)).limit(limit).all():
        results.append({"type": "album", "id": album.id, "label": album.title, "sublabel": album.artist.name})
    for song in db.query(Song).join(Album).join(Artist).filter(Song.title.ilike(like)).limit(limit).all():
        results.append(
            {
                "type": "song",
                "id": song.id,
                "label": song.title,
                "sublabel": f"{song.album.artist.name} · {song.album.title}",
            }
        )
    return results[: limit * 2]


def search_notes(db: Session, q: str, limit: int = 12) -> list[dict]:
    """Search thoughts by title/body for hyperlinking drafts and published posts."""
    like = f"%{q}%"
    rows = (
        db.query(Note)
        .filter((Note.title.ilike(like)) | (Note.body.ilike(like)))
        .order_by(Note.updated_at.desc(), Note.created_at.desc())
        .limit(limit)
        .all()
    )
    items: list[dict] = []
    for note in rows:
        title = (note.title or "").strip() or "Untitled thought"
        items.append(
            {
                "id": note.id,
                "title": title,
                "status": note.status or "published",
                "url": f"/thoughts/{note.id}",
            }
        )
    return items


def related_songs_for_note(db: Session, note_id: int) -> list[dict]:
    rows = (
        db.query(Song)
        .join(NoteSong, NoteSong.song_id == Song.id)
        .join(Song.album)
        .join(Album.artist)
        .filter(NoteSong.note_id == note_id)
        .order_by(Artist.name.asc(), Album.title.asc(), Song.title.asc())
        .all()
    )
    return [
        {
            "id": song.id,
            "title": song.title,
            "artist": song.album.artist.name if song.album and song.album.artist else "",
            "album": song.album.title if song.album else "",
            "url": f"/songs/{song.id}",
        }
        for song in rows
    ]
