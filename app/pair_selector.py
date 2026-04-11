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
from collections import deque
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from .models import Song, Album, PlaylistSong, Playlist, Comparison
from .placement import pick_placement_song, pick_opponent

CANDIDATE_POOL = 60  # sample this many random songs, then score pairs
RECENT_SONG_HISTORY = 8
RECENT_PAIR_HISTORY = 40

# Anti-repeat: track recently shown song IDs (last ~4 comparisons = 8 songs)
_RECENT_SONG_IDS: deque[int] = deque(maxlen=8)
_RECENT_PAIR_KEYS: deque[tuple[int, int]] = deque(maxlen=12)


def _pair_key(song_a_id: int, song_b_id: int) -> tuple[int, int]:
    return tuple(sorted((song_a_id, song_b_id)))


def note_recent_pair(song_a_id: int, song_b_id: int) -> None:
    _RECENT_SONG_IDS.append(song_a_id)
    _RECENT_SONG_IDS.append(song_b_id)
    _RECENT_PAIR_KEYS.append(_pair_key(song_a_id, song_b_id))


def _recent_set() -> set[int]:
    return set(_RECENT_SONG_IDS)


def _recent_pairs() -> set[tuple[int, int]]:
    return set(_RECENT_PAIR_KEYS)


def _db_recent_rows(db: Session, limit: int) -> list[tuple[int, int]]:
    rows = (
        db.query(Comparison.song_a_id, Comparison.song_b_id)
        .order_by(Comparison.id.desc())
        .limit(limit)
        .all()
    )
    return [(int(a), int(b)) for a, b in rows]


def _combined_recent_songs(db: Session) -> set[int]:
    recent = set(_recent_set())
    for a_id, b_id in _db_recent_rows(db, RECENT_SONG_HISTORY):
        recent.add(a_id)
        recent.add(b_id)
    return recent


def _combined_recent_pairs(db: Session) -> set[tuple[int, int]]:
    recent = set(_recent_pairs())
    for a_id, b_id in _db_recent_rows(db, RECENT_PAIR_HISTORY):
        recent.add(_pair_key(a_id, b_id))
    return recent


def _filter_recent(songs: list[Song], recent: set[int]) -> list[Song]:
    if not recent:
        return songs
    filtered = [s for s in songs if s.id not in recent]
    return filtered if len(filtered) >= 2 else songs


def _score_pair(a: Song, b: Song) -> float:
    rd_sum = a.glicko_rd + b.glicko_rd
    rating_diff = abs(a.glicko_rating - b.glicko_rating)
    freshness = 1.0 / (1.0 + min(a.comparison_count, b.comparison_count))
    # normalize: RD max 700, rating_diff penalty over 400
    return (rd_sum / 700.0) * 0.5 + max(0.0, 1.0 - rating_diff / 400.0) * 0.3 + freshness * 0.2


def _best_pair(songs: list[Song], recent_pairs: set[tuple[int, int]] | None = None) -> tuple[Song, Song] | None:
    if len(songs) < 2:
        return None
    best = None
    best_score = -1.0
    recent_pairs = recent_pairs or set()
    # O(n^2) is fine for n<=60
    for i in range(len(songs)):
        for j in range(i + 1, len(songs)):
            if _pair_key(songs[i].id, songs[j].id) in recent_pairs:
                continue
            s = _score_pair(songs[i], songs[j])
            if s > best_score:
                best_score = s
                best = (songs[i], songs[j])
    if best is not None:
        return best
    # If every candidate pair was recently used, allow repeats rather than failing.
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

    recent = _combined_recent_songs(db)
    recent_pairs = _combined_recent_pairs(db)

    # Binary-search placement: interleave so the same in-flight song doesn't
    # appear back-to-back. Only honor placement priority if the candidate
    # wasn't just shown; otherwise fall through to a normal pair this round.
    placement_song = pick_placement_song(db)
    if placement_song is not None and placement_song.id not in recent:
        opponent = pick_opponent(db, placement_song)
        attempts = 0
        while (
            opponent is not None
            and (opponent.id in recent or _pair_key(placement_song.id, opponent.id) in recent_pairs)
            and attempts < 8
        ):
            opponent = pick_opponent(db, placement_song)
            attempts += 1
        if (
            opponent is not None
            and opponent.id not in recent
            and _pair_key(placement_song.id, opponent.id) not in recent_pairs
        ):
            return (placement_song, opponent)

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
        candidates = _filter_recent(candidates, recent)
        pair = _best_pair(candidates, recent_pairs)
        if pair:
            return pair

    # 20% cross-playlist bridging: pick two random playlists, one song each
    if roll < 0.30:
        playlist_ids = [p.id for p in db.query(Playlist.id).all()]
        if len(playlist_ids) >= 2:
            pa, pb = random.sample(playlist_ids, 2)
            sa = _sample_songs_from_playlist(db, pa, CANDIDATE_POOL // 2)
            sb = _sample_songs_from_playlist(db, pb, CANDIDATE_POOL // 2)
            sa = _filter_recent(sa, recent)
            sb = _filter_recent(sb, recent)
            if sa and sb:
                # best cross-pair by score
                best = None
                best_score = -1.0
                for x in sa:
                    for y in sb:
                        if _pair_key(x.id, y.id) in recent_pairs:
                            continue
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
            songs = _filter_recent(songs, recent)
            pair = _best_pair(songs, recent_pairs)
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
    candidates = _filter_recent(candidates, recent)
    return _best_pair(candidates, recent_pairs)


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
