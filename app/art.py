"""Local disk cache for album covers and artist images."""
from pathlib import Path
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent.parent
ART_DIR = ROOT / "data" / "art"
ALBUM_DIR = ART_DIR / "albums"
ARTIST_DIR = ART_DIR / "artists"

ALBUM_DIR.mkdir(parents=True, exist_ok=True)
ARTIST_DIR.mkdir(parents=True, exist_ok=True)


def _download(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "MYKMAN-Music/0.1 (local app)",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        if not data or len(data) < 100:
            return False
        dest.write_bytes(data)
        return True
    except Exception as e:
        print(f"[art] download failed {url}: {e}")
        return False


def cache_album_art(album, db=None) -> str | None:
    if album is None:
        return None
    if album.cover_path:
        p = ART_DIR / album.cover_path
        if p.exists():
            return album.cover_path
    if not album.cover_url:
        return None
    dest = ALBUM_DIR / f"{album.id}.jpg"
    if _download(album.cover_url, dest):
        rel = f"albums/{album.id}.jpg"
        album.cover_path = rel
        if db is not None:
            db.commit()
        return rel
    return None


def cache_artist_image(artist, db=None) -> str | None:
    if artist is None:
        return None
    if artist.image_path:
        p = ART_DIR / artist.image_path
        if p.exists():
            return artist.image_path
    if not artist.image_url:
        return None
    dest = ARTIST_DIR / f"{artist.id}.jpg"
    if _download(artist.image_url, dest):
        rel = f"artists/{artist.id}.jpg"
        artist.image_path = rel
        if db is not None:
            db.commit()
        return rel
    return None
