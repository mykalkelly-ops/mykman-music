import math
import re
from collections import defaultdict, deque

from sqlalchemy.orm import Session

from .models import Song, Album, PlaylistSong, SongLink

_SPACE_RE = re.compile(r"\s+")
_PAREN_RE = re.compile(r"\s*\((single version|album version|explicit|clean|remaster(?:ed)?(?: \d{4})?)\)\s*", re.IGNORECASE)


def normalize_title(title: str | None) -> str:
    text = (title or "").strip().lower()
    text = _PAREN_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text)
    return text.strip()


def canonical_key_from_parts(artist_name: str | None, title: str | None, duration_ms: int | None = None) -> tuple[str, str, int]:
    seconds = int(round((duration_ms or 0) / 1000.0))
    bucket = int(round(seconds / 5.0)) if seconds else 0
    return ((artist_name or "").strip().lower(), normalize_title(title), bucket)


def canonical_key(song: Song) -> tuple[str, str, int]:
    artist_name = ""
    if song.album and song.album.artist:
        artist_name = song.album.artist.name
    return canonical_key_from_parts(artist_name, song.title, song.duration_ms)


def linked_song_groups(db: Session) -> dict[int, int]:
    """Return song_id -> component id for manually linked same-song releases."""
    graph: dict[int, set[int]] = defaultdict(set)
    for left_id, right_id in db.query(SongLink.left_song_id, SongLink.right_song_id).filter(SongLink.relation == "same_song").all():
        graph[left_id].add(right_id)
        graph[right_id].add(left_id)
    groups: dict[int, int] = {}
    seen: set[int] = set()
    for root in graph:
        if root in seen:
            continue
        queue = deque([root])
        component: list[int] = []
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            component.append(cur)
            for nxt in graph.get(cur, ()):
                if nxt not in seen:
                    queue.append(nxt)
        cid = min(component)
        for sid in component:
            groups[sid] = cid
    return groups


def unique_liked_song_count(db: Session) -> int:
    songs = db.query(Song).join(PlaylistSong, PlaylistSong.song_id == Song.id).all()
    groups = linked_song_groups(db)
    seen: set[tuple[str, str, int] | tuple[str, int]] = set()
    for song in songs:
        gid = groups.get(song.id)
        if gid is not None:
            seen.add(("linked", gid))
        else:
            seen.add(canonical_key(song))
    return len(seen)


def progress_metrics(db: Session) -> dict[str, int | float]:
    unique_liked = max(unique_liked_song_count(db), 1)
    completed = db.query(func_count_comparisons()).scalar() or 0
    target = int(math.ceil(unique_liked * max(8, math.log2(unique_liked) * 4)))
    pct = min(100.0, (completed / target * 100.0)) if target else 0.0
    return {
        "completed": completed,
        "target": target,
        "percent": round(pct, 1),
        "unique_liked": unique_liked,
    }


def func_count_comparisons():
    from sqlalchemy import func
    from .models import Comparison
    return func.count(Comparison.id)
