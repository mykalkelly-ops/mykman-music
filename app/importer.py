"""
Import an Apple Music Library.xml (exported from Mac Music app) into the SQLite DB.

Usage:
    python -m app.importer path/to/Library.xml
"""
import plistlib
import re
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from .db import engine, SessionLocal
from .models import (
    Artist, Album, Song, Playlist, PlaylistSong, init_db
)

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

PLAYLIST_NAME_RE = re.compile(
    r"^(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})$",
    re.IGNORECASE,
)


def parse_playlist_name(name: str):
    """Return (month, year) if name matches 'Month YYYY', else (None, None)."""
    if not name:
        return (None, None)
    m = PLAYLIST_NAME_RE.match(name.strip())
    if not m:
        return (None, None)
    return (MONTH_MAP[m.group(1).lower()], int(m.group(2)))


def get_or_create_artist(db: Session, name: str) -> Artist:
    name = (name or "Unknown Artist").strip()
    artist = db.query(Artist).filter_by(name=name).one_or_none()
    if artist is None:
        artist = Artist(name=name)
        db.add(artist)
        db.flush()
    return artist


def get_or_create_album(db: Session, artist: Artist, title: str, year: int | None, genre: str | None) -> Album:
    title = (title or "Unknown Album").strip()
    album = db.query(Album).filter_by(artist_id=artist.id, title=title).one_or_none()
    if album is None:
        album = Album(artist_id=artist.id, title=title, year=year, genre=genre)
        db.add(album)
        db.flush()
    else:
        # Backfill metadata if missing
        if album.year is None and year is not None:
            album.year = year
        if not album.genre and genre:
            album.genre = genre
    return album


def import_library(xml_path: Path) -> dict:
    with open(xml_path, "rb") as f:
        plist = plistlib.load(f)

    tracks = plist.get("Tracks", {})
    playlists = plist.get("Playlists", [])

    init_db(engine)
    db = SessionLocal()
    stats = {"songs": 0, "artists": 0, "albums": 0, "playlists": 0, "playlist_songs": 0, "skipped_playlists": 0}

    try:
        # ---- Tracks ----
        track_id_to_song_pk: dict[str, int] = {}
        artist_cache: dict[str, int] = {}

        for track_id, t in tracks.items():
            # Skip non-music items (podcasts, videos, etc.)
            if t.get("Kind") and "audio" not in str(t.get("Kind", "")).lower() and "matched" not in str(t.get("Kind", "")).lower() and "purchased" not in str(t.get("Kind", "")).lower() and "protected" not in str(t.get("Kind", "")).lower():
                # Be permissive; Apple Music entries often have "Apple Music AAC audio file" or similar
                pass

            name = t.get("Name")
            if not name:
                continue
            artist_name = t.get("Album Artist") or t.get("Artist") or "Unknown Artist"
            album_title = t.get("Album") or "Unknown Album"
            year = t.get("Year")
            genre = t.get("Genre")

            if artist_name in artist_cache:
                artist = db.get(Artist, artist_cache[artist_name])
            else:
                artist = get_or_create_artist(db, artist_name)
                artist_cache[artist_name] = artist.id

            album = get_or_create_album(db, artist, album_title, year, genre)

            song = (
                db.query(Song)
                .filter_by(album_id=album.id, title=name)
                .one_or_none()
            )
            if song is None:
                song = Song(
                    album_id=album.id,
                    title=name,
                    duration_ms=t.get("Total Time"),
                    apple_track_id=str(track_id),
                )
                db.add(song)
                db.flush()
                stats["songs"] += 1
            track_id_to_song_pk[str(track_id)] = song.id

        db.commit()
        stats["artists"] = db.query(Artist).count()
        stats["albums"] = db.query(Album).count()

        # ---- Playlists ----
        for p in playlists:
            pname = p.get("Name", "")
            month, year = parse_playlist_name(pname)
            if month is None:
                stats["skipped_playlists"] += 1
                continue

            playlist = db.query(Playlist).filter_by(name=pname).one_or_none()
            if playlist is None:
                playlist = Playlist(name=pname, month=month, year=year)
                db.add(playlist)
                db.flush()
            stats["playlists"] += 1

            items = p.get("Playlist Items", []) or []
            for item in items:
                tid = str(item.get("Track ID"))
                song_pk = track_id_to_song_pk.get(tid)
                if song_pk is None:
                    continue
                exists = (
                    db.query(PlaylistSong)
                    .filter_by(playlist_id=playlist.id, song_id=song_pk)
                    .one_or_none()
                )
                if exists is None:
                    db.add(PlaylistSong(playlist_id=playlist.id, song_id=song_pk))
                    stats["playlist_songs"] += 1

        db.commit()
    finally:
        db.close()

    return stats


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m app.importer path/to/Library.xml")
        sys.exit(1)
    xml_path = Path(sys.argv[1]).expanduser().resolve()
    if not xml_path.exists():
        print(f"File not found: {xml_path}")
        sys.exit(1)
    print(f"Importing {xml_path} ...")
    stats = import_library(xml_path)
    print("Done.")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
