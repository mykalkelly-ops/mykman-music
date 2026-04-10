"""Genre normalization helpers for analytics and import."""


def normalize_genre(value: str | None) -> str | None:
    if not value:
        return value
    raw = value.strip()
    if not raw:
        return None

    key = raw.casefold().replace("-", "").replace("/", "").replace(" ", "")
    if key in {
        "alternativerap",
        "hiphoprap",
        "undergroundrap",
        "rap",
        "hiphop",
    }:
        return "Hip-Hop/Rap"

    return raw
