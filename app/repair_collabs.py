"""Normalize obvious collaboration artist rows into real song credits.

Example: "Sexyy Red & Chief Keef" remains available as a display/collab act,
but it stops behaving like one fake person. Its songs get primary credits for
Sexyy Red and Chief Keef, and the collab act links to those child artists.
"""
from sqlalchemy.orm import Session

from .artist_names import split_collaboration_artists
from .models import Artist, ArtistMembership, Person, SongCredit, init_db
from .db import engine, SessionLocal


def _get_or_create_artist(db: Session, name: str) -> Artist:
    artist = db.query(Artist).filter(Artist.name.ilike(name)).order_by(Artist.id.asc()).first()
    if artist is None:
        artist = Artist(name=name, kind="solo", prompt_resolved=False)
        db.add(artist)
        db.flush()
    return artist


def _ensure_child_membership(db: Session, parent: Artist, child: Artist) -> bool:
    if parent.id == child.id:
        return False
    existing = (
        db.query(ArtistMembership)
        .filter(
            ArtistMembership.artist_id == parent.id,
            ArtistMembership.child_artist_id == child.id,
        )
        .first()
    )
    if existing is not None:
        return False
    db.add(ArtistMembership(artist_id=parent.id, child_artist_id=child.id, role="member"))
    return True


def _remove_fake_self_person(db: Session, artist: Artist) -> int:
    removed = 0
    rows = (
        db.query(ArtistMembership)
        .join(Person, ArtistMembership.person_id == Person.id)
        .filter(
            ArtistMembership.artist_id == artist.id,
            ArtistMembership.person_id.isnot(None),
            Person.name.ilike(artist.name),
        )
        .all()
    )
    for row in rows:
        db.delete(row)
        removed += 1
    return removed


def run(db: Session) -> dict[str, int]:
    known_names = {name for (name,) in db.query(Artist.name).all()}
    stats = {
        "collab_artists": 0,
        "created_artists": 0,
        "created_memberships": 0,
        "created_credits": 0,
        "removed_fake_people": 0,
        "removed_collab_credits": 0,
    }

    for artist in db.query(Artist).order_by(Artist.id.asc()).all():
        parts = split_collaboration_artists(
            artist.name,
            known_names=known_names - {artist.name},
            require_known_part=True,
        )
        if len(parts) < 2:
            continue

        stats["collab_artists"] += 1
        children: list[Artist] = []
        for part in parts:
            before_count = len(known_names)
            child = _get_or_create_artist(db, part)
            known_names.add(child.name)
            if len(known_names) > before_count:
                stats["created_artists"] += 1
            children.append(child)
            if _ensure_child_membership(db, artist, child):
                stats["created_memberships"] += 1

        artist.kind = "collab"
        artist.gender = "Band"
        artist.is_band = True
        artist.prompt_resolved = True
        stats["removed_fake_people"] += _remove_fake_self_person(db, artist)

        for credit in db.query(SongCredit).filter(SongCredit.artist_id == artist.id).all():
            for child in children:
                exists = (
                    db.query(SongCredit)
                    .filter(
                        SongCredit.song_id == credit.song_id,
                        SongCredit.artist_id == child.id,
                        SongCredit.role == credit.role,
                    )
                    .first()
                )
                if exists is None:
                    db.add(SongCredit(song_id=credit.song_id, artist_id=child.id, role=credit.role))
                    stats["created_credits"] += 1
            db.delete(credit)
            stats["removed_collab_credits"] += 1

    db.commit()
    return stats


def main():
    init_db(engine)
    db = SessionLocal()
    try:
        stats = run(db)
        print("Collab repair complete:")
        for key, value in stats.items():
            print(f"  {key}: {value}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
