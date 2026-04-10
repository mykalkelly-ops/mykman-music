"""
Backfill Person, ArtistMembership, and SongCredit rows from existing Artist/Album/Song.

Idempotent — safe to re-run.

Usage:
    python -m app.backfill_people
"""
from sqlalchemy.orm import Session

from .db import engine, SessionLocal
from .models import (
    Artist, Album, Song, Person, ArtistMembership, SongCredit, init_db,
)


_LEGACY_GENDER_MAP = {
    "M": "male",
    "F": "female",
    "NB": "nonbinary",
    "Band": "unknown",
    "Unknown": "unknown",
    "male": "male",
    "female": "female",
    "nonbinary": "nonbinary",
    "unknown": "unknown",
}


def _map_gender(g):
    if not g:
        return "unknown"
    return _LEGACY_GENDER_MAP.get(g, "unknown")


def run(db: Session) -> dict:
    created_persons = 0
    created_memberships = 0
    created_credits = 0
    updated_kind = 0

    artists = db.query(Artist).all()
    for artist in artists:
        is_band = bool(artist.is_band) or (artist.gender == "Band")
        kind = "group" if is_band else "solo"
        if artist.kind != kind and artist.kind not in ("group", "collab"):
            # Don't override an explicit collab/group choice already set
            if artist.kind != kind:
                artist.kind = kind
                updated_kind += 1

        if not is_band:
            person_gender = _map_gender(artist.gender)
            person = db.query(Person).filter(Person.name == artist.name).first()
            if person is None:
                person = Person(name=artist.name, gender=person_gender)
                db.add(person)
                db.flush()
                created_persons += 1
            # Membership artist -> person
            existing = (
                db.query(ArtistMembership)
                .filter(
                    ArtistMembership.artist_id == artist.id,
                    ArtistMembership.person_id == person.id,
                )
                .first()
            )
            if existing is None:
                db.add(ArtistMembership(
                    artist_id=artist.id,
                    person_id=person.id,
                    role="member",
                ))
                created_memberships += 1

        # SongCredits for every song under every album
        for album in artist.albums:
            for song in album.songs:
                exists = (
                    db.query(SongCredit)
                    .filter(
                        SongCredit.song_id == song.id,
                        SongCredit.artist_id == artist.id,
                        SongCredit.role == "primary",
                    )
                    .first()
                )
                if exists is None:
                    db.add(SongCredit(
                        song_id=song.id,
                        artist_id=artist.id,
                        role="primary",
                    ))
                    created_credits += 1

    db.commit()
    return {
        "created_persons": created_persons,
        "created_memberships": created_memberships,
        "created_credits": created_credits,
        "updated_kind": updated_kind,
    }


def main():
    init_db(engine)
    db = SessionLocal()
    try:
        stats = run(db)
        print("Backfill complete:")
        for k, v in stats.items():
            print(f"  {k}: {v}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
