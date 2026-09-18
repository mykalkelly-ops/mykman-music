"""Batch selection: one library/history snapshot per refill, no retry storm."""
import random
from collections import defaultdict

from sqlalchemy.orm import joinedload

from .models import Song, Album, Comparison, PlaylistSong
from .pair_selector import _best_pair, _pair_key, _score_pair, _play_evidence_score, _recent_set
from .placement import RATING_FLOOR, RATING_CEILING


def select_queue_pairs(db, count, excluded_song_ids=(), excluded_pairs=()):
    songs = db.query(Song).options(joinedload(Song.album).joinedload(Album.artist)).all()
    history = db.query(Comparison.song_a_id, Comparison.song_b_id).order_by(Comparison.id.desc()).all()
    blocked = {_pair_key(a, b) for a, b in history} | set(excluded_pairs)
    recent = _recent_set() | {sid for row in history[:40] for sid in row}
    used = set(excluded_song_ids)
    playlists = defaultdict(set)
    for pid, sid in db.query(PlaylistSong.playlist_id, PlaylistSong.song_id).all():
        playlists[pid].add(sid)
    selected = []
    for _ in range(count):
        available = [song for song in songs if song.id not in used]
        fresh = [song for song in available if song.id not in recent]
        pool = fresh if len(fresh) >= 2 else available
        if len(pool) < 2:
            break
        pair = None
        pending = sorted((s for s in pool if s.placement_pending), key=lambda s: (
            s.placement_lo is None and s.placement_hi is None, -s.comparison_count, s.id))
        placed_pool = [s for s in pool if not s.placement_pending]
        for anchor in pending if placed_pool else []:
            placed = [s for s in placed_pool if _pair_key(s.id, anchor.id) not in blocked]
            if placed:
                target = ((anchor.placement_lo if anchor.placement_lo is not None else RATING_FLOOR)
                          + (anchor.placement_hi if anchor.placement_hi is not None else RATING_CEILING)) / 2
                opponent = (max(placed, key=lambda s: s.glicko_rating)
                            if anchor.placement_lo is None and anchor.placement_hi is None
                            else min(placed, key=lambda s: abs(s.glicko_rating - target)))
                pair = (anchor, opponent)
                break
        roll = random.random()
        if pair is None and roll < .15:
            evidence = [s for s in pool if s.play_count >= 5 and (s.glicko_rd > 145 or s.comparison_count < 8)]
            if evidence:
                anchor = max(evidence, key=_play_evidence_score)
                opponents = [s for s in pool if s.id != anchor.id and _pair_key(s.id, anchor.id) not in blocked]
                if opponents:
                    pair = (anchor, max(opponents, key=lambda s: _score_pair(anchor, s)))
        if pair is None and roll < .25:
            pair = _best_pair(sorted(pool, key=lambda s: (-s.glicko_rd, s.comparison_count))[:60], blocked)
        if pair is None and roll < .45 and len(playlists) >= 2:
            pa, pb = random.sample(list(playlists), 2)
            left = random.sample([s for s in pool if s.id in playlists[pa]], k=min(30, sum(s.id in playlists[pa] for s in pool)))
            right = random.sample([s for s in pool if s.id in playlists[pb]], k=min(30, sum(s.id in playlists[pb] for s in pool)))
            cross = [(a, b) for a in left for b in right if a.id != b.id and _pair_key(a.id, b.id) not in blocked]
            if cross:
                pair = max(cross, key=lambda p: _score_pair(*p))
        if pair is None and playlists:
            for pid in random.sample(list(playlists), min(5, len(playlists))):
                members = [s for s in pool if s.id in playlists[pid]]
                pair = _best_pair(random.sample(members, min(60, len(members))), blocked)
                if pair:
                    break
        if pair is None:
            pair = _best_pair(random.sample(pool, min(60, len(pool))), blocked)
        if pair is None:
            # Relax recency, never historical exclusions. Scan lazily to avoid
            # allocating all N*(N-1)/2 pairs when a sampled pool is exhausted.
            pair = next(((a, b) for i, a in enumerate(available) for b in available[i+1:]
                         if _pair_key(a.id, b.id) not in blocked), None)
        if pair is None:
            break
        selected.append(pair)
        used.update(s.id for s in pair)
        blocked.add(_pair_key(*(s.id for s in pair)))
    return selected
