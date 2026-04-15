"""
Import an Apple Music Library.xml (exported from Mac Music app) into the SQLite DB.

Usage:
    python -m app.importer path/to/Library.xml
"""
import plistlib
import re
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from .db import engine, SessionLocal
from .models import Artist, Album, Song, Playlist, PlaylistSong, SongCredit, init_db
from .dedupe import merge_case_duplicates
from .history import backup_before_import
from .genres import normalize_genre
from .scoring import is_various_artists_name

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

PLAYLIST_NAME_RE = re.compile(
    r"^(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})$",
    re.IGNORECASE,
)
FEAT_RE = re.compile(r"\((?:feat\.?|featuring|ft\.?)\s+([^)]+)\)", re.IGNORECASE)


def parse_playlist_name(name: str):
    if not name:
        return (None, None)
    match = PLAYLIST_NAME_RE.match(name.strip())
    if not match:
        return (None, None)
    return (MONTH_MAP[match.group(1).lower()], int(match.group(2)))


def parse_featured_artists(title: str | None) -> list[str]:
    text = title or ""
    names: list[str] = []
    for chunk in FEAT_RE.findall(text):
        parts = re.split(r"\s*(?:,|&| and )\s*", chunk, flags=re.IGNORECASE)
        for part in parts:
            value = (part or "").strip()
            if value and value not in names:
                names.append(value)
    return names


def album_key(track: dict) -> tuple[str, str]:
    artist_name = (track.get("Album Artist") or track.get("Artist") or "Unknown Artist").strip()
    album_title = (track.get("Album") or "Unknown Album").strip()
    return (artist_name.lower(), album_title.lower())


def get_or_create_artist(db: Session, name: str) -> Artist:
    name = (name or "Unknown Artist").strip()
    artist = db.query(Artist).filter(Artist.name.ilike(name)).one_or_none()
    if artist is None:
        artist = Artist(name=name)
        db.add(artist)
        db.flush()
    return artist


def get_or_create_album(db: Session, artist: Artist, title: str, year: int | None, genre: str | None) -> Album:
    title = (title or "Unknown Album").strip()
    album = db.query(Album).filter(Album.artist_id == artist.id, Album.title.ilike(title)).one_or_none()
    if album is None:
        album = Album(artist_id=artist.id, title=title, year=year, genre=genre)
        db.add(album)
        db.flush()
    else:
        if album.year is None and year is not None:
            album.year = year
        if not album.genre and genre:
            album.genre = genre
    return album


def ensure_song_credit(db: Session, song: Song, artist: Artist, role: str = "primary") -> None:
    existing = (
        db.query(SongCredit)
        .filter(SongCredit.song_id == song.id, SongCredit.artist_id == artist.id, SongCredit.role == role)
        .one_or_none()
    )
    if existing is None:
        db.add(SongCredit(song_id=song.id, artist_id=artist.id, role=role))


def import_library(xml_path: Path) -> dict:
    backup_path = backup_before_import()
    with open(xml_path, "rb") as handle:
        plist = plistlib.load(handle)

    tracks = plist.get("Tracks", {})
    playlists = plist.get("Playlists", [])

    init_db(engine)
    db = SessionLocal()
    stats = {
        "songs": 0,
        "artists": 0,
        "albums": 0,
        "playlists": 0,
        "playlist_songs": 0,
        "skipped_playlists": 0,
        "backup": backup_path or "",
    }

    try:
        wanted_track_ids: set[str] = set()
        listened_album_keys: set[tuple[str, str]] = set()

        for playlist in playlists:
            if parse_playlist_name(playlist.get("Name", ""))[0] is None:
                continue
            for item in (playlist.get("Playlist Items", []) or []):
                track_id = item.get("Track ID")
                if track_id is None:
                    continue
                tid = str(track_id)
                wanted_track_ids.add(tid)
                track = tracks.get(tid) or tracks.get(track_id)
                if track:
                    listened_album_keys.add(album_key(track))

        track_id_to_song_pk: dict[str, int] = {}
        artist_cache: dict[str, int] = {}

        for track_id, track in tracks.items():
            if album_key(track) not in listened_album_keys:
                continue

            name = track.get("Name")
            if not name:
                continue

            artist_name = track.get("Album Artist") or track.get("Artist") or "Unknown Artist"
            album_title = track.get("Album") or "Unknown Album"
            year = track.get("Year")
            genre = normalize_genre(track.get("Genre"))

            cache_key = artist_name.strip()
            if cache_key in artist_cache:
                artist = db.get(Artist, artist_cache[cache_key])
            else:
                artist = get_or_create_artist(db, artist_name)
                artist_cache[cache_key] = artist.id

            album = get_or_create_album(db, artist, album_title, year, genre)
            song = (
                db.query(Song)
                .filter(Song.album_id == album.id, Song.title.ilike(name))
                .one_or_none()
            )
            if song is None:
                song = Song(
                    album_id=album.id,
                    title=name,
                    track_number=track.get("Track Number"),
                    duration_ms=track.get("Total Time"),
                    apple_track_id=str(track_id),
                    liked=str(track_id) in wanted_track_ids,
                )
                db.add(song)
                db.flush()
                stats["songs"] += 1
            else:
                if song.apple_track_id is None:
                    song.apple_track_id = str(track_id)
                if song.track_number is None and track.get("Track Number") is not None:
                    song.track_number = track.get("Track Number")
                if str(track_id) in wanted_track_ids:
                    song.liked = True

            primary_artist_name = track.get("Artist") or artist_name
            if is_various_artists_name(artist.name) and primary_artist_name:
                primary_artist = get_or_create_artist(db, primary_artist_name)
                ensure_song_credit(db, song, primary_artist, "primary")
            else:
                ensure_song_credit(db, song, artist, "primary")
            for featured_name in parse_featured_artists(name):
                feat_artist = get_or_create_artist(db, featured_name)
                ensure_song_credit(db, song, feat_artist, "featured")

            track_id_to_song_pk[str(track_id)] = song.id

        db.commit()
        stats["artists"] = db.query(Artist).count()
        stats["albums"] = db.query(Album).count()

        for playlist in playlists:
            playlist_name = playlist.get("Name", "")
            month, year = parse_playlist_name(playlist_name)
            if month is None:
                stats["skipped_playlists"] += 1
                continue

            row = db.query(Playlist).filter_by(name=playlist_name).one_or_none()
            if row is None:
                row = Playlist(name=playlist_name, month=month, year=year)
                db.add(row)
                db.flush()
            stats["playlists"] += 1

            seen_song_pks: set[int] = {
                song_id for (song_id,) in db.query(PlaylistSong.song_id).filter_by(playlist_id=row.id).all()
            }
            for item in (playlist.get("Playlist Items", []) or []):
                track_id = str(item.get("Track ID"))
                song_pk = track_id_to_song_pk.get(track_id)
                if song_pk is None or song_pk in seen_song_pks:
                    continue
                seen_song_pks.add(song_pk)
                db.add(PlaylistSong(playlist_id=row.id, song_id=song_pk))
                stats["playlist_songs"] += 1

        db.commit()
        merge_case_duplicates(db)
        stats["artists"] = db.query(Artist).count()
        stats["albums"] = db.query(Album).count()
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
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
