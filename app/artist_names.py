import re


_COLLAB_SPLIT_RE = re.compile(
    r"\s*(?:,|&|\+|×|\bx\b|\band\b|\bwith\b)\s*",
    re.IGNORECASE,
)
_HAS_STRONG_SEPARATOR_RE = re.compile(
    r"\s(?:&|\+|×|x|and|with)\s",
    re.IGNORECASE,
)

_PROTECTED_ARTIST_NAMES = {
    "captain beefheart & his magic band",
    "earth, wind & fire",
    "edward sharpe & the magnetic zeros",
    "nick cave & the bad seeds",
    "tom petty and the heartbreakers",
    "mumford & sons",
}

_PROTECTED_COLLAB_TAILS = {
    "fire",
    "sons",
    "his magic band",
    "her band",
    "their band",
    "the heartbreakers",
    "the bad seeds",
    "the magnetic zeros",
}


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def split_collaboration_artists(
    name: str | None,
    known_names: set[str] | None = None,
    *,
    require_known_part: bool = False,
) -> list[str]:
    """Split obvious joint artist display strings into credited acts.

    This is intentionally conservative. A lot of real band names contain "&" or
    "and", so imports can require at least one already-known part before
    splitting. Admin/repair paths can be looser because they run after the
    library has accumulated artist rows.
    """
    raw = (name or "").strip()
    if not raw:
        return []

    lowered = _norm(raw)
    if lowered in _PROTECTED_ARTIST_NAMES:
        return [raw]
    if not _HAS_STRONG_SEPARATOR_RE.search(raw):
        return [raw]
    if " and the " in lowered:
        return [raw]

    parts = [part.strip() for part in _COLLAB_SPLIT_RE.split(raw) if part.strip()]
    if len(parts) < 2:
        return [raw]

    for part in parts[1:]:
        if _norm(part) in _PROTECTED_COLLAB_TAILS:
            return [raw]
        if _norm(part).startswith(("his ", "her ", "their ")):
            return [raw]

    known = {_norm(value) for value in (known_names or set())}
    if require_known_part and known and not any(_norm(part) in known for part in parts):
        return [raw]

    # Preserve order while de-duping exact/case-only repeats.
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        key = _norm(part)
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    return out or [raw]
