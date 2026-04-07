"""
Active-learning pair selection.

Scoring each candidate pair by information value:
  - High combined RD (uncertain ratings) = more informative
  - Similar current rating = more informative (not a blowout)
  - Neither song compared recently = prefer fresh matchups
  - Prefer songs with low comparison_count (give everything coverage)

Strategy:
  - 70% intra-playlist (small pools, fast convergence)
  - 20% cross-playlist bridging
  - 10% new-song priority (songs with very high RD / zero comparisons)
"""
import random
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from .models import Song, Album, PlaylistSong, Playlist

CANDIDATE_POOL = 60  # sample this many random songs, then score pairs


def _score_pair(a: Song, b: Song) -> float:
    rd_sum = a.glicko_rd + b.glicko_rd
    rating_diff = abs(a.glicko_rating - b.glicko_rating)
    freshness = 1.0 / (1.0 + min(a.comparison_count, b.comparison_count))
    # normalize: RD max 700, rating_diff penalty over 400
    return (rd_sum / 700.0) * 0.5 + max(0.0, 1.0 - rating_diff / 400.0) * 0.3 + freshness * 0.2


def _best_pair(songs: list[Song]) -> tuple[Song, Song] | None:
    if len(songs) < 2:
        return None
    best = None
    best_score = -1.0
    # O(n^2) is fine for n<=60
    for i in range(len(songs)):
        for j in range(i + 1, len(songs)):
            s = _score_pair(songs[i], songs[j])
            if s > best_score:
                best_score = s
                best = (songs[i], songs[j])
    return best


def pick_pair(db: Session) -> tuple[Song, Song] | None:
    total_songs = db.query(func.count(Song.id)).scalar() or 0
    if total_songs < 2:
        return None

    roll = random.random()

    # 10% new / very uncertain
    if roll < 0.10:
        candidates = (
            db.query(Song)
            .options(joinedload(Song.album).joinedload(Album.artist))
            .order_by(Song.glicko_rd.desc(), Song.comparison_count.asc())
            .limit(CANDIDATE_POOL)
            .all()
        )
        pair = _best_pair(candidates)
        if pair:
            return pair

    # 20% cross-playlist bridging: pick two random playlists, one song each
    if roll < 0.30:
        playlist_ids = [p.id for p in db.query(Playlist.id).all()]
        if len(playlist_ids) >= 2:
            pa, pb = random.sample(playlist_ids, 2)
            sa = _sample_songs_from_playlist(db, pa, CANDIDATE_POOL // 2)
            sb = _sample_songs_from_playlist(db, pb, CANDIDATE_POOL // 2)
            if sa and sb:
                # best cross-pair by score
                best = None
                best_score = -1.0
                for x in sa:
                    for y in sb:
                        s = _score_pair(x, y)
                        if s > best_score:
                            best_score = s
                            best = (x, y)
                if best:
                    return best

    # 70% intra-playlist (default path)
    playlist_ids = [p.id for p in db.query(Playlist.id).all()]
    if playlist_ids:
        for _ in range(5):
            pid = random.choice(playlist_ids)
            songs = _sample_songs_from_playlist(db, pid, CANDIDATE_POOL)
            pair = _best_pair(songs)
            if pair:
                return pair

    # Fallback: two random songs from the whole library
    candidates = (
        db.query(Song)
        .options(joinedload(Song.album).joinedload(Album.artist))
        .order_by(func.random())
        .limit(CANDIDATE_POOL)
        .all()
    )
    return _best_pair(candidates)


def _sample_songs_from_playlist(db: Session, playlist_id: int, n: int) -> list[Song]:
    rows = (
        db.query(Song)
        .options(joinedload(Song.album).joinedload(Album.artist))
        .join(PlaylistSong, PlaylistSong.song_id == Song.id)
        .filter(PlaylistSong.playlist_id == playlist_id)
        .order_by(func.random())
        .limit(n)
        .all()
    )
    return rows
