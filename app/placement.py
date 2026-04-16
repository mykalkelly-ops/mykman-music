"""
Binary-search placement for new songs.

Goal: place a new song in the rating order using as few comparisons as possible.

Algorithm:
1. First comparison: new song vs the highest-rated *placed* song.
   - If new wins  -> it's now the new #1. Set rating above current top, exit placement.
   - If new loses -> upper bound = top rating. Move to step 2.
2. Subsequent comparisons: pick an opponent whose rating is at the midpoint
   of the current [lo, hi] bounds.
   - If new wins  -> lo = opponent.rating
   - If new loses -> hi = opponent.rating
3. Exit placement when:
   - hi - lo < CONVERGENCE_GAP, OR
   - comparison_count >= MAX_PLACEMENT_COMPARISONS
   Final rating = midpoint of (lo, hi).

Once placed, the song joins the normal pool and Glicko-2 refines it further.
"""
import random
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from .models import Song

CONVERGENCE_GAP = 100.0
MAX_PLACEMENT_COMPARISONS = 10
RATING_FLOOR = 600.0
RATING_CEILING = 2400.0
PLACEMENT_NEIGHBORHOOD = 12


def pick_placement_song(db: Session) -> Song | None:
    """Return a placement-pending song, preferring ones already in progress
    so we finish placing a song before starting another.

    Priority:
      1. Pending songs with bounds already set (finish in-flight placements)
      2. Pending songs with no bounds yet (start a new placement)
    """
    in_flight = (
        db.query(Song)
        .filter(Song.placement_pending == True)  # noqa: E712
        .filter((Song.placement_lo.isnot(None)) | (Song.placement_hi.isnot(None)))
        .order_by(Song.comparison_count.desc())
        .first()
    )
    if in_flight is not None:
        return in_flight
    return (
        db.query(Song)
        .filter(Song.placement_pending == True)  # noqa: E712
        .order_by(Song.id.asc())
        .first()
    )


def pick_opponent(
    db: Session,
    song: Song,
    exclude_song_ids: set[int] | None = None,
    exclude_pair_keys: set[tuple[int, int]] | None = None,
) -> Song | None:
    """Pick the next opponent for a placement-pending song based on its bounds."""
    exclude_song_ids = set(exclude_song_ids or set())
    exclude_song_ids.add(song.id)
    exclude_pair_keys = set(exclude_pair_keys or set())
    lo = song.placement_lo
    hi = song.placement_hi

    # Round 1: compare against current top-rated placed song
    if lo is None and hi is None:
        top_candidates = (
            db.query(Song)
            .filter(~Song.id.in_(exclude_song_ids))
            .filter(Song.placement_pending == False)  # noqa: E712
            .order_by(Song.glicko_rating.desc())
            .limit(PLACEMENT_NEIGHBORHOOD)
            .all()
        )
        random.shuffle(top_candidates)
        for candidate in top_candidates:
            if tuple(sorted((song.id, candidate.id))) not in exclude_pair_keys:
                return candidate
        return (
            db.query(Song)
            .filter(~Song.id.in_(exclude_song_ids))
            .first()
        ) or (
            # cold start: no placed songs yet, just grab any other song
            db.query(Song)
            .filter(~Song.id.in_(exclude_song_ids))
            .order_by(func.random())
            .first()
        )

    # Compute target rating at midpoint of bounds
    lo_b = lo if lo is not None else RATING_FLOOR
    hi_b = hi if hi is not None else RATING_CEILING
    target = (lo_b + hi_b) / 2.0

    # Find the placed song closest to target rating
    neighborhood = (
        db.query(Song)
        .filter(~Song.id.in_(exclude_song_ids))
        .filter(Song.placement_pending == False)  # noqa: E712
        .order_by(func.abs(Song.glicko_rating - target))
        .limit(PLACEMENT_NEIGHBORHOOD)
        .all()
    )
    random.shuffle(neighborhood)
    for candidate in neighborhood:
        if tuple(sorted((song.id, candidate.id))) not in exclude_pair_keys:
            return candidate

    opponent = (
        db.query(Song)
        .filter(~Song.id.in_(exclude_song_ids))
        .filter(Song.placement_pending == False)  # noqa: E712
        .order_by(func.abs(Song.glicko_rating - target))
        .first()
    )
    if opponent is None:
        opponent = (
            db.query(Song)
            .filter(~Song.id.in_(exclude_song_ids))
            .order_by(func.abs(Song.glicko_rating - target))
            .first()
        )
    return opponent


def update_bounds(song: Song, opponent: Song, song_won: bool) -> None:
    """Update placement bounds on `song` after a comparison against `opponent`."""
    opp_rating = opponent.glicko_rating
    if song_won:
        # song is better than opponent -> raise lower bound
        if song.placement_lo is None or opp_rating > song.placement_lo:
            song.placement_lo = opp_rating
    else:
        # song is worse than opponent -> lower upper bound
        if song.placement_hi is None or opp_rating < song.placement_hi:
            song.placement_hi = opp_rating


def maybe_finalize(song: Song) -> None:
    """If the song's bounds have converged (or it's hit the cap), exit placement."""
    lo = song.placement_lo
    hi = song.placement_hi

    if song.comparison_count >= MAX_PLACEMENT_COMPARISONS:
        _finalize(song)
        return

    # Beat the current top? Promote above and finalize.
    if lo is not None and hi is None and lo >= RATING_CEILING - 100:
        _finalize(song)
        return

    # Sank below the bottom? Finalize.
    if hi is not None and lo is None and hi <= RATING_FLOOR + 100:
        _finalize(song)
        return

    if lo is not None and hi is not None and (hi - lo) < CONVERGENCE_GAP:
        _finalize(song)
        return


def _finalize(song: Song) -> None:
    lo = song.placement_lo
    hi = song.placement_hi
    if lo is not None and hi is not None:
        song.glicko_rating = (lo + hi) / 2.0
    elif lo is not None:
        song.glicko_rating = min(lo + 100.0, RATING_CEILING)
    elif hi is not None:
        song.glicko_rating = max(hi - 100.0, RATING_FLOOR)
    # shrink RD a bit since we have real data now
    song.glicko_rd = min(song.glicko_rd, 200.0)
    song.placement_pending = False
