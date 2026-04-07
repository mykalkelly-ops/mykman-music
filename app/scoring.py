"""
Derived scoring for albums and artists, and 5-star tier assignment.

Philosophy:
- Song ratings come directly from Glicko-2.
- Album score = weighted mean of its songs' ratings, where songs NOT in any
  playlist are treated as "listened but not liked" and use a low anchor rating.
  This lets albums with half-liked tracklists outrank ones with one liked banger.
- Artist score = weighted mean of their album scores, weighted by song count.
- 5-star tiers use fixed Glicko rating cutoffs. Songs with high RD are "unrated"
  until the system has enough data to be confident.

Scores are computed on demand (not stored) so they always reflect the latest
Glicko state. We can cache later if it gets slow.
"""
from dataclasses import dataclass
from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Artist, Album, Song, PlaylistSong

# Anchor rating for songs that exist in the library but aren't in any playlist
# (interpretation: "listened to but didn't like enough to add")
UNLIKED_ANCHOR = 1200.0

# Max RD at which we'll assign a star tier. Above this the song is "unrated".
TIER_RD_THRESHOLD = 120.0

# Fixed Glicko cutoffs. Tune these after ~500 real comparisons.
TIER_CUTOFFS = [
    (1850, 5),
    (1700, 4),
    (1500, 3),
    (1300, 2),
    (0, 1),
]


def star_tier(rating: float, rd: float) -> int | None:
    """Return 1-5 stars, or None if RD is too high to be confident."""
    if rd > TIER_RD_THRESHOLD:
        return None
    for cutoff, stars in TIER_CUTOFFS:
        if rating >= cutoff:
            return stars
    return 1


@dataclass
class AlbumScore:
    album_id: int
    title: str
    artist_name: str
    score: float
    song_count: int
    liked_count: int  # songs in at least one playlist
    avg_rd: float


@dataclass
class ArtistScore:
    artist_id: int
    name: str
    score: float
    album_count: int
    total_songs: int
    liked_songs: int


def _song_effective_rating(song: Song, is_liked: bool) -> float:
    if is_liked:
        return song.glicko_rating
    # For unliked songs we use the anchor UNLESS the song has been directly
    # compared and rated lower than the anchor (rare but possible).
    return min(UNLIKED_ANCHOR, song.glicko_rating)


def album_scores(db: Session) -> list[AlbumScore]:
    # Pull liked song ids in one query
    liked_ids = {
        sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()
    }

    results: list[AlbumScore] = []
    albums = db.query(Album).all()
    for album in albums:
        songs = album.songs
        if not songs:
            continue
        total = 0.0
        weight_sum = 0.0
        liked_count = 0
        rd_sum = 0.0
        for s in songs:
            is_liked = s.id in liked_ids
            if is_liked:
                liked_count += 1
            eff = _song_effective_rating(s, is_liked)
            # RD-based confidence weight: low RD = high trust
            w = max(0.1, 1.0 - (s.glicko_rd / 350.0))
            total += eff * w
            weight_sum += w
            rd_sum += s.glicko_rd
        score = total / weight_sum if weight_sum else 0.0
        results.append(AlbumScore(
            album_id=album.id,
            title=album.title,
            artist_name=album.artist.name if album.artist else "",
            score=score,
            song_count=len(songs),
            liked_count=liked_count,
            avg_rd=rd_sum / len(songs),
        ))
    results.sort(key=lambda x: x.score, reverse=True)
    return results


def artist_scores(db: Session) -> list[ArtistScore]:
    album_map: dict[int, list[AlbumScore]] = {}
    for a in album_scores(db):
        # need artist id — re-query in a map
        pass
    # Easier: compute directly here.
    liked_ids = {
        sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()
    }
    results: list[ArtistScore] = []
    artists = db.query(Artist).all()
    for artist in artists:
        total_weight = 0.0
        score_acc = 0.0
        total_songs = 0
        liked_songs = 0
        album_count = 0
        for album in artist.albums:
            if not album.songs:
                continue
            album_count += 1
            album_total = 0.0
            album_weight = 0.0
            for s in album.songs:
                total_songs += 1
                is_liked = s.id in liked_ids
                if is_liked:
                    liked_songs += 1
                eff = _song_effective_rating(s, is_liked)
                w = max(0.1, 1.0 - (s.glicko_rd / 350.0))
                album_total += eff * w
                album_weight += w
            if album_weight == 0:
                continue
            album_score = album_total / album_weight
            # weight each album by its song count
            album_w = len(album.songs)
            score_acc += album_score * album_w
            total_weight += album_w
        if total_weight == 0:
            continue
        score = score_acc / total_weight
        results.append(ArtistScore(
            artist_id=artist.id,
            name=artist.name,
            score=score,
            album_count=album_count,
            total_songs=total_songs,
            liked_songs=liked_songs,
        ))
    results.sort(key=lambda x: x.score, reverse=True)
    return results
