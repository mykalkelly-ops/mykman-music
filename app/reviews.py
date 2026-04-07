"""
Review prompting: surface songs / albums / artists the user clearly loves
(confident high ratings) but hasn't written a note about yet.
"""
from sqlalchemy.orm import Session

from .models import Song, Album, Artist, Note
from .scoring import album_scores, artist_scores, star_tier, TIER_RD_THRESHOLD

# "Loved" thresholds. Tunable as the library grows.
LOVED_SONG_RATING = 1800.0
LOVED_SONG_MAX_RD = 120.0
LOVED_ALBUM_SCORE = 1700.0
LOVED_ARTIST_SCORE = 1650.0

MAX_PROMPTS_PER_KIND = 10


def _reviewed_ids(db: Session, target_type: str) -> set[int]:
    return {
        tid for (tid,) in db.query(Note.target_id)
        .filter(Note.target_type == target_type)
        .filter(Note.target_id.isnot(None))
        .distinct()
        .all()
    }


def loved_songs_needing_review(db: Session) -> list[dict]:
    reviewed = _reviewed_ids(db, "song")
    rows = (
        db.query(Song)
        .filter(Song.glicko_rating >= LOVED_SONG_RATING)
        .filter(Song.glicko_rd <= LOVED_SONG_MAX_RD)
        .order_by(Song.glicko_rating.desc())
        .all()
    )
    out = []
    for s in rows:
        if s.id in reviewed:
            continue
        out.append({
            "id": s.id,
            "title": s.title,
            "artist": s.album.artist.name if s.album and s.album.artist else "",
            "album": s.album.title if s.album else "",
            "rating": round(s.glicko_rating, 0),
            "stars": star_tier(s.glicko_rating, s.glicko_rd) or 5,
        })
        if len(out) >= MAX_PROMPTS_PER_KIND:
            break
    return out


def loved_albums_needing_review(db: Session) -> list[dict]:
    reviewed = _reviewed_ids(db, "album")
    out = []
    for a in album_scores(db):
        if a.score < LOVED_ALBUM_SCORE:
            break  # already sorted desc
        if a.album_id in reviewed:
            continue
        out.append({
            "id": a.album_id,
            "title": a.title,
            "artist": a.artist_name,
            "score": round(a.score, 0),
        })
        if len(out) >= MAX_PROMPTS_PER_KIND:
            break
    return out


def loved_artists_needing_review(db: Session) -> list[dict]:
    reviewed = _reviewed_ids(db, "artist")
    out = []
    for a in artist_scores(db):
        if a.score < LOVED_ARTIST_SCORE:
            break
        if a.artist_id in reviewed:
            continue
        out.append({
            "id": a.artist_id,
            "name": a.name,
            "score": round(a.score, 0),
            "liked_songs": a.liked_songs,
        })
        if len(out) >= MAX_PROMPTS_PER_KIND:
            break
    return out


def any_review_candidate(db: Session) -> dict | None:
    """Return a single highest-priority review prompt for inline display."""
    songs = loved_songs_needing_review(db)
    if songs:
        s = songs[0]
        return {"kind": "song", "id": s["id"], "label": f'{s["title"]} — {s["artist"]}'}
    albums = loved_albums_needing_review(db)
    if albums:
        a = albums[0]
        return {"kind": "album", "id": a["id"], "label": f'{a["title"]} — {a["artist"]}'}
    artists = loved_artists_needing_review(db)
    if artists:
        ar = artists[0]
        return {"kind": "artist", "id": ar["id"], "label": ar["name"]}
    return None
