"""
MusicBrainz + Wikidata + Cover Art Archive integration.

Uses stdlib urllib only. Rate-limited to 1 request/second (MusicBrainz policy).
All network calls are wrapped in try/except and return None / [] / {} on failure.
"""
import json
import time
import threading
import re
import urllib.parse
import urllib.request
import urllib.error

MB_BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "MYKMAN-Music/0.1 (local app)"

_lock = threading.Lock()
_last_request = 0.0
MIN_INTERVAL = 1.05  # seconds


def _rate_limit():
    global _last_request
    with _lock:
        now = time.time()
        delta = now - _last_request
        if delta < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - delta)
        _last_request = time.time()


def _get_json(url: str, timeout: float = 15.0) -> dict | None:
    _rate_limit()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return json.loads(data.decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"[mb] GET failed {url}: {e}")
        return None


def search_artist(name: str) -> list[dict]:
    q = urllib.parse.quote(f'artist:"{name}"')
    url = f"{MB_BASE}/artist?query={q}&fmt=json&limit=5"
    data = _get_json(url)
    if not data:
        return []
    out = []
    for a in data.get("artists", [])[:5]:
        out.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "country": a.get("country"),
            "gender": a.get("gender"),
            "type": a.get("type"),
            "disambiguation": a.get("disambiguation"),
            "life-span": a.get("life-span") or {},
            "score": a.get("score"),
        })
    return out


def get_artist(mbid: str) -> dict | None:
    url = f"{MB_BASE}/artist/{mbid}?inc=url-rels+artist-rels+release-groups&fmt=json"
    return _get_json(url)


def search_release_group(artist_name: str, album_name: str) -> dict | None:
    preferred_types = _preferred_release_types(album_name)
    for title in _album_title_variants(album_name):
        q = urllib.parse.quote(f'releasegroup:"{title}" AND artist:"{artist_name}"')
        url = f"{MB_BASE}/release-group?query={q}&fmt=json&limit=5"
        data = _get_json(url)
        if not data:
            continue
        groups = data.get("release-groups") or []
        if not groups:
            continue
        for primary_type in preferred_types:
            for g in groups:
                if (g.get("primary-type") or "").lower() == primary_type:
                    return g
        return groups[0]
    return None


def search_release(artist_name: str, album_name: str) -> dict | None:
    preferred_types = _preferred_release_types(album_name)
    for title in _album_title_variants(album_name):
        q = urllib.parse.quote(f'release:"{title}" AND artist:"{artist_name}"')
        url = f"{MB_BASE}/release?query={q}&fmt=json&limit=10"
        data = _get_json(url)
        if not data:
            continue
        releases = data.get("releases") or []
        if not releases:
            continue
        for preferred_type in preferred_types:
            for release in releases:
                rg = release.get("release-group") or {}
                if (rg.get("primary-type") or "").lower() == preferred_type and (release.get("status") or "").lower() in ("official", ""):
                    return release
        for release in releases:
            if (release.get("status") or "").lower() in ("official", ""):
                return release
        return releases[0]
    return None


def _album_title_variants(album_name: str) -> list[str]:
    value = (album_name or "").strip()
    variants: list[str] = []

    def add(v: str):
        v = (v or "").strip()
        if v and v not in variants:
            variants.append(v)

    add(value)
    add(re.sub(r"\s*-\s*EP\s*$", " EP", value, flags=re.I))
    add(re.sub(r"\s*-\s*Single\s*$", " Single", value, flags=re.I))
    add(re.sub(r"\s*-\s*(EP|Single)\s*$", "", value, flags=re.I))
    add(re.sub(r"\s*\((Deluxe|Deluxe Edition|Deluxe Version|Expanded|Remastered|Remaster|Bonus.*?|EP|Single)\)\s*$", "", value, flags=re.I))
    add(re.sub(r"\s*\[(Deluxe|Deluxe Edition|Deluxe Version|Expanded|Remastered|Remaster|Bonus.*?|EP|Single)\]\s*$", "", value, flags=re.I))
    add(re.sub(r"\s+(Deluxe|Deluxe Edition|Deluxe Version|Expanded|Remastered|Remaster)\s*$", "", value, flags=re.I))
    return variants


def _preferred_release_types(album_name: str) -> list[str]:
    title = (album_name or "").lower()
    if "ep" in title:
        return ["ep", "album", "single"]
    if "single" in title:
        return ["single", "ep", "album"]
    return ["album", "ep", "single"]


def get_release(mbid: str) -> dict | None:
    url = f"{MB_BASE}/release/{mbid}?inc=recordings&fmt=json"
    return _get_json(url)


def get_release_group(mbid: str) -> dict | None:
    url = f"{MB_BASE}/release-group/{mbid}?inc=artist-credits&fmt=json"
    return _get_json(url)


def get_cover_art_url(release_group_mbid: str) -> str:
    return f"https://coverartarchive.org/release-group/{release_group_mbid}/front-500"


def get_wikidata_image_url(artist_mbid: str) -> str | None:
    """Follow MB url-rels -> wikidata -> P18 image -> Commons file path URL."""
    try:
        data = get_artist(artist_mbid)
        if not data:
            return None
        qid = None
        for rel in data.get("relations", []) or []:
            if rel.get("type") == "wikidata":
                url = (rel.get("url") or {}).get("resource") or ""
                # e.g. https://www.wikidata.org/wiki/Q12345
                if "/wiki/" in url:
                    qid = url.rsplit("/wiki/", 1)[-1].strip()
                    if qid:
                        break
        if not qid:
            return None
        wd_url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
        # Wikidata is not subject to MB rate limit but we'll share it anyway.
        wd = _get_json(wd_url)
        if not wd:
            return None
        entities = wd.get("entities") or {}
        entity = entities.get(qid) or {}
        claims = (entity.get("claims") or {}).get("P18") or []
        if not claims:
            return None
        filename = (
            (((claims[0] or {}).get("mainsnak") or {}).get("datavalue") or {}).get("value")
        )
        if not filename:
            return None
        return (
            "https://commons.wikimedia.org/wiki/Special:FilePath/"
            + urllib.parse.quote(filename) + "?width=500"
        )
    except Exception as e:
        print(f"[mb] wikidata image failed for {artist_mbid}: {e}")
        return None
