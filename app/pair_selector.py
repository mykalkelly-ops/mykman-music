"""Pair scoring and recent-display hints. Batch selection lives in queue_selection."""
import math
from collections import deque
from sqlalchemy.orm import Session
from .models import Song

PLAY_EVIDENCE_MIN_PLAY_COUNT = 5
_RECENT_SONG_IDS: deque[int] = deque(maxlen=80)
_RECENT_PAIR_KEYS: deque[tuple[int, int]] = deque(maxlen=500)


def _pair_key(song_a_id: int, song_b_id: int) -> tuple[int, int]:
    return tuple(sorted((song_a_id, song_b_id)))


def note_recent_pair(song_a_id: int, song_b_id: int) -> None:
    _RECENT_SONG_IDS.append(song_a_id)
    _RECENT_SONG_IDS.append(song_b_id)
    _RECENT_PAIR_KEYS.append(_pair_key(song_a_id, song_b_id))


def _recent_set() -> set[int]:
    return set(_RECENT_SONG_IDS)


def _score_pair(a: Song, b: Song) -> float:
    rd_sum = a.glicko_rd + b.glicko_rd
    rating_diff = abs(a.glicko_rating - b.glicko_rating)
    freshness = 1.0 / (1.0 + min(a.comparison_count, b.comparison_count))
    # normalize: RD max 700, rating_diff penalty over 400
    return (rd_sum / 700.0) * 0.5 + max(0.0, 1.0 - rating_diff / 400.0) * 0.3 + freshness * 0.2


def _play_evidence_score(song: Song) -> float:
    play_count = max(0, int(song.play_count or 0))
    skip_count = max(0, int(song.skip_count or 0))
    if play_count < PLAY_EVIDENCE_MIN_PLAY_COUNT:
        return 0.0
    uncertainty = max(0.0, min(1.0, song.glicko_rd / 350.0))
    under_compared = 1.0 / (1.0 + max(0, song.comparison_count or 0))
    skip_drag = min(0.25, skip_count / max(1, play_count + skip_count) * 0.25)
    return math.log1p(play_count) * ((uncertainty * 0.6) + (under_compared * 0.4)) * (1.0 - skip_drag)


def _best_pair(songs: list[Song], recent_pairs: set[tuple[int, int]] | None = None) -> tuple[Song, Song] | None:
    if len(songs) < 2:
        return None
    best = None
    best_score = -1.0
    recent_pairs = recent_pairs or set()
    # O(n^2) is fine for n<=60
    for i in range(len(songs)):
        for j in range(i + 1, len(songs)):
            if songs[i].id == songs[j].id or _pair_key(songs[i].id, songs[j].id) in recent_pairs:
                continue
            s = _score_pair(songs[i], songs[j])
            if s > best_score:
                best_score = s
                best = (songs[i], songs[j])
    return best


def pick_pair(db: Session) -> tuple[Song, Song] | None:
    """Compatibility entry point sharing the queue's history exclusions."""
    from .queue_selection import select_queue_pairs
    pairs = select_queue_pairs(db, 1)
    return pairs[0] if pairs else None
