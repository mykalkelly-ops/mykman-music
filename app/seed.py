"""
Seed the DB with fake data for testing Phase 2 before a real Library.xml import.

Usage:
    python -m app.seed
"""
import random
from datetime import datetime

from .db import engine, SessionLocal
from .models import Artist, Album, Song, Playlist, PlaylistSong, init_db

ARTIST_NAMES = [
    "The Midnight Echoes", "Violet Shore", "Ocean Parallel", "Kira Sato",
    "Northwind", "Paperhouse", "Lumen", "Dust Cartel", "Hollow Moon",
    "Azure Room", "Low Tide", "Ember & Oak", "Static Parade", "Marigold Dive",
    "Cold Signal", "Feral Rivers", "Blueprint Kids", "Orchid Motel",
    "Candela", "Grey Collective", "Maplewood", "Silent Alley", "Riverbend",
    "Neon Monastery", "Small Engine", "The Outside Set", "Harbor Lantern",
    "Foxglove", "Warm Static", "Long Division",
]
GENRES = ["Indie", "Electronic", "Rock", "Hip-Hop", "Folk", "Pop", "R&B", "Ambient", "Jazz", "Metal"]
WORDS = ["light", "sea", "hollow", "morning", "ghost", "garden", "river", "broken", "fire", "glass",
         "quiet", "neon", "dream", "shadow", "wander", "bloom", "silver", "paper", "distant", "pale"]


def _song_title() -> str:
    n = random.choice([1, 2, 2, 3])
    return " ".join(random.choice(WORDS) for _ in range(n)).title()


def _album_title() -> str:
    return _song_title()


def seed(n_artists: int = 30, albums_per_artist: int = 3, songs_per_album: int = 10):
    random.seed(42)
    init_db(engine)
    db = SessionLocal()
    try:
        if db.query(Artist).count() > 0:
            print("DB already has data — refusing to seed. Delete data/music.db to reseed.")
            return

        artists = []
        for name in ARTIST_NAMES[:n_artists]:
            a = Artist(name=name, gender=random.choice(["M", "F", "Band", None]))
            db.add(a)
            artists.append(a)
        db.flush()

        all_songs: list[Song] = []
        for artist in artists:
            for _ in range(albums_per_artist):
                album = Album(
                    artist_id=artist.id,
                    title=_album_title(),
                    year=random.randint(1998, 2026),
                    genre=random.choice(GENRES),
                )
                db.add(album)
                db.flush()
                for _ in range(songs_per_album):
                    s = Song(album_id=album.id, title=_song_title(), duration_ms=random.randint(120000, 300000))
                    db.add(s)
                    all_songs.append(s)
        db.flush()

        # Create monthly playlists for the last 12 months and dump ~40 random songs into each
        months = [
            ("January", 1), ("February", 2), ("March", 3), ("April", 4),
            ("May", 5), ("June", 6), ("July", 7), ("August", 8),
            ("September", 9), ("October", 10), ("November", 11), ("December", 12),
        ]
        year = 2025
        for i, (mname, mnum) in enumerate(months):
            p = Playlist(name=f"{mname} {year}", month=mnum, year=year)
            db.add(p)
            db.flush()
            picks = random.sample(all_songs, k=min(40, len(all_songs)))
            for s in picks:
                db.add(PlaylistSong(playlist_id=p.id, song_id=s.id, added_at=datetime.utcnow()))

        db.commit()
        print(f"Seeded: {len(artists)} artists, {db.query(Album).count()} albums, {len(all_songs)} songs, 12 playlists.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
