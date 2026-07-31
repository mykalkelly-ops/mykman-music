import json
import os
import re
import shutil
import secrets as _secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from urllib.parse import parse_qs

from fastapi import FastAPI, Depends, Request, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .db import engine, get_session
from .models import (
    Artist, Album, Song, SongLink, Playlist, PlaylistSong, Comparison, Note, Comment,
    Person, ArtistMembership, SongCredit, Subscriber, AlbumTrack, ListenQueueItem, NoteSong, ArtistRelease,
    ComparisonQueueItem, init_db,
)
from .auth import (
    is_admin, require_admin, login as do_login, logout as do_logout,
    is_subscriber, unlock_subscriber, lock_subscriber,
)
from .glicko import update_pair
from .pair_selector import pick_pair, note_recent_pair
from .placement import update_bounds, maybe_finalize
from .scoring import album_scores, album_score_for, artist_scores, artist_score_for, top_artist_scores, myk_tier, myk_score, render_myks, gender_breakdown, is_rankable_album, classify_release_type, effective_album_total_tracks, _expand_artist_genders, is_various_artists_name
from .notes import render_markdown, resolve_target, search_notes, search_targets, related_songs_for_note
from .canonical import canonical_key, unique_liked_song_count, progress_metrics, linked_song_groups
from .genres import normalize_genre
from .dedupe import repair_known_artist_data
from .reviews import (
    loved_songs_needing_review,
    loved_albums_needing_review,
    any_review_candidate,
)
from .history import append_event
from .history import (
    BACKUP_DIR,
    JOURNAL_PATH,
    DB_PATH,
    comparison_count_in_db,
    export_comparisons_from_db,
    snapshot_db,
)
from .paths import data_dir
from .apple_music import get_config as get_apple_music_config, generate_developer_token

app = FastAPI(title="MYKMAN Music")

ARTIST_SCORES_CACHE_TTL_SECONDS = 300
ARTIST_SCORES_CACHE: dict[str, object] = {"created_at": 0.0, "rows": None}
ARTIST_SCORES_CACHE_LOCK = threading.Lock()
TODAY_CACHE_TTL_SECONDS = 300
TODAY_VALUE_CACHE: dict[str, dict[str, object]] = {
    "review_albums": {"created_at": 0.0, "value": None},
    "progress": {"created_at": 0.0, "value": None},
    "gender_breakdown": {"created_at": 0.0, "value": None},
    "listened_song_count": {"created_at": 0.0, "value": None},
    "liked_song_count": {"created_at": 0.0, "value": None},
    "album_scores": {"created_at": 0.0, "value": None},
}
TODAY_VALUE_CACHE_LOCK = threading.Lock()


def invalidate_artist_scores_cache() -> None:
    with ARTIST_SCORES_CACHE_LOCK:
        ARTIST_SCORES_CACHE["created_at"] = 0.0
        ARTIST_SCORES_CACHE["rows"] = None
    with TODAY_VALUE_CACHE_LOCK:
        for row in TODAY_VALUE_CACHE.values():
            row["created_at"] = 0.0
            row["value"] = None


def cached_artist_scores(db: Session, refresh: bool = False):
    now = time.monotonic()
    with ARTIST_SCORES_CACHE_LOCK:
        rows = ARTIST_SCORES_CACHE.get("rows")
        created_at = float(ARTIST_SCORES_CACHE.get("created_at") or 0.0)
        if not refresh and rows is not None and now - created_at < ARTIST_SCORES_CACHE_TTL_SECONDS:
            return rows

    rows = artist_scores(db)
    with ARTIST_SCORES_CACHE_LOCK:
        ARTIST_SCORES_CACHE["created_at"] = time.monotonic()
        ARTIST_SCORES_CACHE["rows"] = rows
    return rows


def _cached_today_value(key: str, compute):
    now = time.monotonic()
    with TODAY_VALUE_CACHE_LOCK:
        cached = TODAY_VALUE_CACHE[key]
        value = cached.get("value")
        created_at = float(cached.get("created_at") or 0.0)
        if value is not None and now - created_at < TODAY_CACHE_TTL_SECONDS:
            return value

    value = compute()
    with TODAY_VALUE_CACHE_LOCK:
        TODAY_VALUE_CACHE[key]["created_at"] = time.monotonic()
        TODAY_VALUE_CACHE[key]["value"] = value
    return value


def cached_loved_albums_needing_review(db: Session) -> list[dict]:
    return _cached_today_value("review_albums", lambda: loved_albums_needing_review(db))


def cached_progress_metrics(db: Session) -> dict[str, int | float]:
    return _cached_today_value("progress", lambda: progress_metrics(db))


def cached_gender_breakdown(db: Session) -> list[tuple[str, int, float]]:
    return _cached_today_value("gender_breakdown", lambda: gender_breakdown(db))


def cached_listened_song_count(db: Session) -> int:
    return _cached_today_value("listened_song_count", lambda: _listened_song_count(db))


def cached_liked_song_count(db: Session) -> int:
    return _cached_today_value("liked_song_count", lambda: _liked_song_count(db))


def cached_album_scores(db: Session, refresh: bool = False):
    if refresh:
        with TODAY_VALUE_CACHE_LOCK:
            TODAY_VALUE_CACHE["album_scores"]["created_at"] = 0.0
            TODAY_VALUE_CACHE["album_scores"]["value"] = None
    return _cached_today_value("album_scores", lambda: album_scores(db))

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Serve cached art from data/art/
ART_DIR = data_dir() / "art"
ART_DIR.mkdir(parents=True, exist_ok=True)
(ART_DIR / "albums").mkdir(parents=True, exist_ok=True)
(ART_DIR / "artists").mkdir(parents=True, exist_ok=True)
app.mount("/art", StaticFiles(directory=str(ART_DIR)), name="art")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

LIBRARY_IMPORT_STATUS = {
    "running": False,
    "done": False,
    "error": None,
    "stats": None,
    "filename": None,
    "started_at": None,
    "finished_at": None,
}


KOFI_URL = os.environ.get("KOFI_URL", "https://ko-fi.com/mykman")
KOFI_VERIFICATION_TOKEN = os.environ.get("KOFI_VERIFICATION_TOKEN", "")


COUNTRY_CENTROIDS = {
    "US": {"name": "United States", "lat": 39.8283, "lon": -98.5795},
    "GB": {"name": "United Kingdom", "lat": 55.3781, "lon": -3.4360},
    "CA": {"name": "Canada", "lat": 56.1304, "lon": -106.3468},
    "FR": {"name": "France", "lat": 46.2276, "lon": 2.2137},
    "PR": {"name": "Puerto Rico", "lat": 18.2208, "lon": -66.5901},
    "JP": {"name": "Japan", "lat": 36.2048, "lon": 138.2529},
    "SE": {"name": "Sweden", "lat": 60.1282, "lon": 18.6435},
    "CO": {"name": "Colombia", "lat": 4.5709, "lon": -74.2973},
    "IT": {"name": "Italy", "lat": 41.8719, "lon": 12.5674},
    "JM": {"name": "Jamaica", "lat": 18.1096, "lon": -77.2975},
    "IE": {"name": "Ireland", "lat": 53.1424, "lon": -7.6921},
    "ES": {"name": "Spain", "lat": 40.4637, "lon": -3.7492},
    "DE": {"name": "Germany", "lat": 51.1657, "lon": 10.4515},
    "AU": {"name": "Australia", "lat": -25.2744, "lon": 133.7751},
    "NO": {"name": "Norway", "lat": 60.4720, "lon": 8.4689},
    "NG": {"name": "Nigeria", "lat": 9.0820, "lon": 8.6753},
    "IS": {"name": "Iceland", "lat": 64.9631, "lon": -19.0208},
    "CV": {"name": "Cabo Verde", "lat": 16.5388, "lon": -23.0418},
    "BR": {"name": "Brazil", "lat": -14.2350, "lon": -51.9253},
    "VE": {"name": "Venezuela", "lat": 6.4238, "lon": -66.5897},
    "NZ": {"name": "New Zealand", "lat": -40.9006, "lon": 174.8860},
    "NL": {"name": "Netherlands", "lat": 52.1326, "lon": 5.2913},
    "MX": {"name": "Mexico", "lat": 23.6345, "lon": -102.5528},
    "KR": {"name": "South Korea", "lat": 35.9078, "lon": 127.7669},
    "CL": {"name": "Chile", "lat": -35.6751, "lon": -71.5430},
    "CH": {"name": "Switzerland", "lat": 46.8182, "lon": 8.2275},
    "AR": {"name": "Argentina", "lat": -38.4161, "lon": -63.6167},
    "CU": {"name": "Cuba", "lat": 21.5218, "lon": -77.7812},
    "DO": {"name": "Dominican Republic", "lat": 18.7357, "lon": -70.1627},
    "HT": {"name": "Haiti", "lat": 18.9712, "lon": -72.2852},
    "ZA": {"name": "South Africa", "lat": -30.5595, "lon": 22.9375},
    "ET": {"name": "Ethiopia", "lat": 9.1450, "lon": 40.4897},
    "GH": {"name": "Ghana", "lat": 7.9465, "lon": -1.0232},
    "KE": {"name": "Kenya", "lat": -0.0236, "lon": 37.9062},
    "RU": {"name": "Russia", "lat": 61.5240, "lon": 105.3188},
    "UA": {"name": "Ukraine", "lat": 48.3794, "lon": 31.1656},
    "PL": {"name": "Poland", "lat": 51.9194, "lon": 19.1451},
    "PT": {"name": "Portugal", "lat": 39.3999, "lon": -8.2245},
    "BE": {"name": "Belgium", "lat": 50.5039, "lon": 4.4699},
    "DK": {"name": "Denmark", "lat": 56.2639, "lon": 9.5018},
    "FI": {"name": "Finland", "lat": 61.9241, "lon": 25.7482},
    "CN": {"name": "China", "lat": 35.8617, "lon": 104.1954},
    "IN": {"name": "India", "lat": 20.5937, "lon": 78.9629},
    "IR": {"name": "Iran", "lat": 32.4279, "lon": 53.6880},
    "TR": {"name": "Turkey", "lat": 38.9637, "lon": 35.2433},
}


CITY_CENTROIDS = {
    ("atlanta", "ga"): {"lat": 33.7490, "lon": -84.3880, "region": "South"},
    ("chicago", "il"): {"lat": 41.8781, "lon": -87.6298, "region": "Midwest"},
    ("detroit", "mi"): {"lat": 42.3314, "lon": -83.0458, "region": "Midwest"},
    ("new york", "ny"): {"lat": 40.7128, "lon": -74.0060, "region": "Northeast"},
    ("brooklyn", "ny"): {"lat": 40.6782, "lon": -73.9442, "region": "Northeast"},
    ("queens", "ny"): {"lat": 40.7282, "lon": -73.7949, "region": "Northeast"},
    ("los angeles", "ca"): {"lat": 34.0522, "lon": -118.2437, "region": "West"},
    ("compton", "ca"): {"lat": 33.8958, "lon": -118.2201, "region": "West"},
    ("long beach", "ca"): {"lat": 33.7701, "lon": -118.1937, "region": "West"},
    ("oakland", "ca"): {"lat": 37.8044, "lon": -122.2712, "region": "West"},
    ("san francisco", "ca"): {"lat": 37.7749, "lon": -122.4194, "region": "West"},
    ("seattle", "wa"): {"lat": 47.6062, "lon": -122.3321, "region": "West"},
    ("portland", "or"): {"lat": 45.5152, "lon": -122.6784, "region": "West"},
    ("houston", "tx"): {"lat": 29.7604, "lon": -95.3698, "region": "South"},
    ("dallas", "tx"): {"lat": 32.7767, "lon": -96.7970, "region": "South"},
    ("austin", "tx"): {"lat": 30.2672, "lon": -97.7431, "region": "South"},
    ("new orleans", "la"): {"lat": 29.9511, "lon": -90.0715, "region": "South"},
    ("miami", "fl"): {"lat": 25.7617, "lon": -80.1918, "region": "South"},
    ("baltimore", "md"): {"lat": 39.2904, "lon": -76.6122, "region": "South"},
    ("washington", "dc"): {"lat": 38.9072, "lon": -77.0369, "region": "South"},
    ("philadelphia", "pa"): {"lat": 39.9526, "lon": -75.1652, "region": "Northeast"},
    ("boston", "ma"): {"lat": 42.3601, "lon": -71.0589, "region": "Northeast"},
    ("minneapolis", "mn"): {"lat": 44.9778, "lon": -93.2650, "region": "Midwest"},
    ("cleveland", "oh"): {"lat": 41.4993, "lon": -81.6944, "region": "Midwest"},
    ("cincinnati", "oh"): {"lat": 39.1031, "lon": -84.5120, "region": "Midwest"},
    ("memphis", "tn"): {"lat": 35.1495, "lon": -90.0490, "region": "South"},
    ("nashville", "tn"): {"lat": 36.1627, "lon": -86.7816, "region": "South"},
    ("london", ""): {"lat": 51.5072, "lon": -0.1276, "region": "United Kingdom"},
    ("manchester", ""): {"lat": 53.4808, "lon": -2.2426, "region": "United Kingdom"},
    ("paris", ""): {"lat": 48.8566, "lon": 2.3522, "region": "France"},
    ("toronto", ""): {"lat": 43.6532, "lon": -79.3832, "region": "Canada"},
    ("montreal", ""): {"lat": 45.5019, "lon": -73.5674, "region": "Canada"},
    ("vancouver", ""): {"lat": 49.2827, "lon": -123.1207, "region": "Canada"},
    ("tokyo", ""): {"lat": 35.6762, "lon": 139.6503, "region": "Japan"},
    ("stockholm", ""): {"lat": 59.3293, "lon": 18.0686, "region": "Sweden"},
    ("kingston", ""): {"lat": 18.0179, "lon": -76.8099, "region": "Jamaica"},
    ("san juan", ""): {"lat": 18.4655, "lon": -66.1057, "region": "Puerto Rico"},
}


US_STATE_REGIONS = {
    "CT": "Northeast", "ME": "Northeast", "MA": "Northeast", "NH": "Northeast", "RI": "Northeast",
    "VT": "Northeast", "NJ": "Northeast", "NY": "Northeast", "PA": "Northeast",
    "IL": "Midwest", "IN": "Midwest", "MI": "Midwest", "OH": "Midwest", "WI": "Midwest",
    "IA": "Midwest", "KS": "Midwest", "MN": "Midwest", "MO": "Midwest", "NE": "Midwest",
    "ND": "Midwest", "SD": "Midwest",
    "DE": "South", "FL": "South", "GA": "South", "MD": "South", "NC": "South", "SC": "South",
    "VA": "South", "DC": "South", "WV": "South", "AL": "South", "KY": "South", "MS": "South",
    "TN": "South", "AR": "South", "LA": "South", "OK": "South", "TX": "South",
    "AZ": "West", "CO": "West", "ID": "West", "MT": "West", "NV": "West", "NM": "West",
    "UT": "West", "WY": "West", "AK": "West", "CA": "West", "HI": "West", "OR": "West", "WA": "West",
}


def _clean_region(value: str | None) -> str:
    text = (value or "").strip()
    if "," in text:
        text = text.split(",")[-1].strip()
    return text


def _city_lookup(city: str | None, region: str | None):
    city_key = (city or "").strip().lower()
    region_clean = _clean_region(region)
    region_key = region_clean.lower()
    candidates = []
    if region_clean:
        candidates.extend([(city_key, region_clean.upper()), (city_key, region_key)])
    candidates.append((city_key, ""))
    for key in candidates:
        if key in CITY_CENTROIDS:
            return CITY_CENTROIDS[key]
    return None


def _us_region(country: str | None, region: str | None, city: str | None = None) -> str | None:
    if (country or "").strip().upper() != "US":
        return None
    region_clean = _clean_region(region).upper()
    if region_clean in US_STATE_REGIONS:
        return US_STATE_REGIONS[region_clean]
    city_hit = _city_lookup(city, region)
    if city_hit:
        return city_hit.get("region")
    return None


@app.middleware("http")
async def inject_admin_flag(request: Request, call_next):
    # make is_admin available to all templates via request.state
    request.state.is_admin = is_admin(request)
    request.state.kofi_url = KOFI_URL
    return await call_next(request)


# Inject kofi_url into every template render via Jinja global
templates.env.globals["kofi_url"] = KOFI_URL


# ---------- Paywall helpers ----------

_WORDS = [
    "velvet", "ember", "lavender", "hollow", "echo", "static", "bruise", "honey",
    "vapor", "lullaby", "moth", "ribbon", "violet", "ghost", "bloom", "drift",
    "neon", "sorrow", "halo", "siren", "aching", "tender", "wisp", "amber",
    "dusk", "rust", "feather", "petal", "marrow", "smoke", "willow", "linen",
    "candle", "thorn", "sugar", "midnight", "silver", "cinder", "saint", "maple",
]


def _generate_code(db: Session) -> str:
    for _ in range(50):
        a = _secrets.choice(_WORDS)
        b = _secrets.choice(_WORDS)
        n = _secrets.randbelow(900) + 100
        code = f"{a}-{b}-{n}"
        if not db.query(Subscriber).filter(Subscriber.access_code == code).first():
            return code
    # fallback
    return f"code-{_secrets.token_urlsafe(8)}"


_TAG_RE = re.compile(r"<[^>]+>")


def _teaser(body: str, n: int = 240) -> str:
    plain = _TAG_RE.sub("", render_markdown(body or "")).strip()
    plain = re.sub(r"\s+", " ", plain)
    return plain[:n]


def _listened_album_ids(db: Session) -> set[int]:
    rows = db.query(Album.id).filter(
        (Album.excluded_from_listened != True) & (
            (Album.confirmed_listened == True) | (Album.id.in_(db.query(Song.album_id).join(PlaylistSong, PlaylistSong.song_id == Song.id).distinct()))  # noqa: E712
        )
    ).all()
    return {album_id for (album_id,) in rows}


def _listened_song_count(db: Session) -> int:
    album_ids = _listened_album_ids(db)
    if not album_ids:
        return 0
    albums = (
        db.query(Album)
        .options(joinedload(Album.artist), joinedload(Album.songs))
        .filter(Album.id.in_(album_ids))
        .all()
    )
    groups = linked_song_groups(db)
    total = 0
    for album in albums:
        effective_total = effective_album_total_tracks(album)
        if effective_total:
            total += int(effective_total)
            continue
        seen: set[tuple[str, str, int] | tuple[str, int]] = set()
        for song in album.songs:
            gid = groups.get(song.id)
            if gid is not None:
                seen.add(("linked", gid))
            else:
                seen.add(canonical_key(song))
        total += len(seen)
    return total


def _liked_song_count(db: Session) -> int:
    return unique_liked_song_count(db)


def _song_external_links(song: Song) -> dict[str, str]:
    artist = song.album.artist.name if song.album and song.album.artist else ""
    query = quote_plus(" ".join(part for part in [song.title, artist] if part).strip())
    return {
        "spotify": f"https://open.spotify.com/search/{query}",
        "apple_music": f"https://music.apple.com/us/search?term={query}",
        "youtube": f"https://www.youtube.com/results?search_query={query}",
    }


def _album_confirmation_candidates(db: Session, limit: int = 100) -> list[Album]:
    liked_artist_ids = {
        aid
        for (aid,) in db.query(Album.artist_id)
        .join(Song, Song.album_id == Album.id)
        .join(PlaylistSong, PlaylistSong.song_id == Song.id)
        .distinct()
        .all()
    }
    if not liked_artist_ids:
        return []
    playlist_album_ids = {
        aid for (aid,) in db.query(Song.album_id).join(PlaylistSong, PlaylistSong.song_id == Song.id).distinct().all()
    }
    query = (
        db.query(Album)
        .options(joinedload(Album.artist), joinedload(Album.songs))
        .filter(
            Album.artist_id.in_(liked_artist_ids),
            Album.confirmed_listened != True,  # noqa: E712
            Album.excluded_from_listened != True,  # noqa: E712
        )
    )
    if playlist_album_ids:
        query = query.filter(~Album.id.in_(playlist_album_ids))
    rows = query.order_by(Album.artist_id.asc(), Album.year.asc().nullslast(), Album.title.asc()).limit(limit).all()
    return rows


def _song_link_map(db: Session, song_ids: set[int]) -> dict[int, list[Song]]:
    if not song_ids:
        return {}
    pairs = (
        db.query(SongLink)
        .filter(
            SongLink.relation == "same_song",
            (SongLink.left_song_id.in_(song_ids)) | (SongLink.right_song_id.in_(song_ids)),
        )
        .all()
    )
    linked_ids: set[int] = set()
    by_song: dict[int, set[int]] = {sid: set() for sid in song_ids}
    for row in pairs:
        by_song.setdefault(row.left_song_id, set()).add(row.right_song_id)
        by_song.setdefault(row.right_song_id, set()).add(row.left_song_id)
        linked_ids.add(row.left_song_id)
        linked_ids.add(row.right_song_id)
    songs = {}
    if linked_ids:
        songs = {
            s.id: s
            for s in db.query(Song)
            .options(joinedload(Song.album).joinedload(Album.artist))
            .filter(Song.id.in_(linked_ids))
            .all()
        }
    out: dict[int, list[Song]] = {}
    for sid, others in by_song.items():
        out[sid] = [songs[oid] for oid in sorted(others) if oid in songs]
    return out


def _apply_comparison_modifiers(
    a: Song,
    b: Song,
    winner_id: int | None,
    difficulty: str | None,
    nostalgia: bool,
):
    if winner_id is None:
        score_a = 0.5
    elif winner_id == a.id:
        score_a = 1.0
    else:
        score_a = 0.0

    (a_rating, a_rd, a_vol), (b_rating, b_rd, b_vol) = update_pair(
        a.glicko_rating, a.glicko_rd, a.glicko_vol,
        b.glicko_rating, b.glicko_rd, b.glicko_vol,
        score_a,
    )

    # Keep this intentionally gentle and tunable.
    diff_mult = {"easy": 1.12, "hard": 0.9}.get(difficulty or "", 1.0)
    nostalgia_mult = 0.92 if nostalgia else 1.0
    mult = diff_mult * nostalgia_mult

    def blend(old_rating: float, new_rating: float) -> float:
        return old_rating + ((new_rating - old_rating) * mult)

    a.glicko_rating = blend(a.glicko_rating, a_rating)
    b.glicko_rating = blend(b.glicko_rating, b_rating)
    a.glicko_rd, a.glicko_vol = a_rd, a_vol
    b.glicko_rd, b.glicko_vol = b_rd, b_vol
    return score_a


# Make is_admin available inside every template
def _ctx(request: Request, **extra):
    base = {"is_admin": getattr(request.state, "is_admin", False)}
    base.update(extra)
    return base


@app.on_event("startup")
def on_startup():
    init_db(engine)
    # Auto-run people backfill on first startup (when persons table is empty)
    from .db import SessionLocal
    db = SessionLocal()
    try:
        if db.query(func.count(Person.id)).scalar() == 0:
            from .backfill_people import run as run_backfill
            stats = run_backfill(db)
            print(f"[startup] backfilled people: {stats}")
        from .repair_collabs import run as run_collab_repair
        collab_stats = run_collab_repair(db)
        if any(collab_stats.values()):
            print(f"[startup] repaired collab artists: {collab_stats}")
        from .dedupe import merge_known_artist_aliases
        alias_stats = merge_known_artist_aliases(db)
        if any(alias_stats.values()):
            print(f"[startup] merged known artist aliases: {alias_stats}")
    finally:
        db.close()


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return {"ok": True}


@app.head("/", response_class=HTMLResponse)
def index_head():
    return HTMLResponse(status_code=200)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_session)):
    total_songs = _listened_song_count(db)
    total_artists = db.query(func.count(Artist.id)).scalar() or 0
    total_albums = db.query(func.count(Album.id)).scalar() or 0
    total_playlists = db.query(func.count(Playlist.id)).scalar() or 0
    total_playlist_songs = _liked_song_count(db)

    playlists = (
        db.query(Playlist)
        .order_by(Playlist.year.desc().nullslast(), Playlist.month.desc().nullslast())
        .all()
    )

    review_songs = loved_songs_needing_review(db) if is_admin(request) else []
    review_albums = cached_loved_albums_needing_review(db) if is_admin(request) else []
    # Artist-wide review prompts currently require a full-library scoring pass.
    # Keep them off the homepage so a normal page load stays lightweight on Render.
    review_artists = []
    progress = cached_progress_metrics(db)
    recent_query = db.query(Note)
    if not is_admin(request):
        recent_query = recent_query.filter(Note.status == "published")
    recent_notes = recent_query.order_by(Note.created_at.desc()).limit(5).all()
    why_note = (
        db.query(Note)
        .filter(
            Note.target_type == "general",
            func.lower(Note.title) == "why i am doing this project",
        )
        .order_by(Note.id.desc())
        .first()
    )
    album_queue_count = len(_album_confirmation_candidates(db, limit=500))
    backup_count = len(list(BACKUP_DIR.glob("*.db"))) if BACKUP_DIR.exists() else 0
    recent_items = []
    for n in recent_notes:
        comment_count = (
            db.query(func.count(Comment.id))
            .filter(Comment.note_id == n.id, Comment.approved == True)  # noqa: E712
            .scalar()
            or 0
        )
        recent_items.append(
            {
                "id": n.id,
                "title": n.title or "Untitled",
                "created_at": n.created_at,
                "target": resolve_target(db, n.target_type, n.target_id),
                "comment_count": comment_count,
            }
        )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "total_songs": total_songs,
            "total_artists": total_artists,
            "total_albums": total_albums,
            "total_playlists": total_playlists,
            "total_playlist_songs": total_playlist_songs,
            "playlists": playlists,
            "review_songs": review_songs,
            "review_albums": review_albums,
            "review_artists": review_artists,
            "progress": progress,
            "recent_notes": recent_items,
            "why_note": why_note,
            "album_queue_count": album_queue_count,
            "backup_count": backup_count,
        },
    )


def _latest_comparison_export_info() -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    exports = sorted(BACKUP_DIR.glob("comparisons-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not exports:
        return {"name": None, "count": None, "mtime": None, "size": None}
    latest = exports[0]
    count = None
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
        count = int(payload.get("comparison_count")) if payload.get("comparison_count") is not None else None
    except Exception:
        count = None
    return {
        "name": latest.name,
        "count": count,
        "mtime": datetime.fromtimestamp(latest.stat().st_mtime),
        "size": latest.stat().st_size,
    }


def _listen_queue_preview(db: Session, limit: int = 5) -> list[dict]:
    rows = db.query(ListenQueueItem).order_by(ListenQueueItem.created_at.desc()).limit(limit).all()
    items = []
    for row in rows:
        label = "Unknown"
        href = "#"
        subtitle = ""
        if row.target_type == "album":
            album = db.get(Album, row.target_id)
            if album:
                label = album.title
                href = f"/albums/{album.id}"
                subtitle = album.artist.name if album.artist else ""
        elif row.target_type == "artist":
            artist = db.get(Artist, row.target_id)
            if artist:
                label = artist.name
                href = f"/artists/{artist.id}"
        items.append(
            {
                "id": row.id,
                "target_type": row.target_type,
                "label": label,
                "href": href,
                "subtitle": subtitle,
                "note": row.note or "",
                "created_at": row.created_at,
            }
        )
    return items


def _next_unresolved_artist(db: Session) -> dict | None:
    artist = (
        db.query(Artist)
        .join(Album, Album.artist_id == Artist.id)
        .join(Song, Song.album_id == Album.id)
        .join(PlaylistSong, PlaylistSong.song_id == Song.id)
        .filter(func.lower(Artist.name) != "various artists")
        .filter((Artist.prompt_resolved.is_(None)) | (Artist.prompt_resolved != True))  # noqa: E712
        .order_by(Artist.name.asc())
        .first()
    )
    if artist is None:
        return None
    reason = "Resolve act type, members, or gender metadata"
    if artist.kind is None:
        reason = "Set act type"
    elif artist.kind == "solo" and (artist.gender is None or artist.gender == "Unknown"):
        reason = "Set solo artist gender"
    elif artist.kind in ("group", "collab"):
        reason = "Add members or child acts"
    return {
        "id": artist.id,
        "label": artist.name,
        "href": f"/artists/{artist.id}#edit-act",
        "kind": artist.kind,
        "gender": artist.gender,
        "reason": reason,
    }


def _next_album_confirmation(db: Session) -> dict | None:
    albums = _album_confirmation_candidates(db, limit=1)
    if not albums:
        return None
    album = albums[0]
    return {
        "id": album.id,
        "label": album.title,
        "artist": album.artist.name if album.artist else "",
        "year": album.year,
        "song_count": len(album.songs),
        "href": f"/albums/{album.id}",
    }


def _review_action_from_candidate(candidate: dict | None) -> dict | None:
    if candidate is None:
        return None
    return {
        "kind": candidate["kind"],
        "id": candidate["id"],
        "label": candidate["label"],
        "href": f"/notes/new?target_type={candidate['kind']}&target_id={candidate['id']}&kind=review&status=draft",
    }


def _artist_name_in_title(artist_name: str, title: str | None) -> bool:
    artist_key = re.sub(r"[^a-z0-9]+", " ", (artist_name or "").lower()).strip()
    title_key = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return bool(artist_key and artist_key in title_key)


def _data_issue_report(db: Session) -> dict[str, list[dict]]:
    artist_rows = cached_artist_scores(db)
    impossible_counts = [
        {
            "artist_id": row.artist_id,
            "name": row.name,
            "liked_songs": row.liked_songs,
            "total_songs": row.total_songs,
            "listened_tracks": row.listened_tracks,
            "known_tracks": row.known_tracks,
        }
        for row in artist_rows
        if row.known_tracks is not None and row.listened_tracks > row.known_tracks
    ]

    leading_conjunction_artists = [
        {"artist_id": artist.id, "name": artist.name}
        for artist in db.query(Artist).filter(Artist.name.ilike("and %")).order_by(Artist.name.asc()).all()
    ]

    feature_only_unchecked = []
    feature_only_no_match = []
    for artist in db.query(Artist).order_by(Artist.name.asc()).all():
        if is_various_artists_name(artist.name) or artist.albums:
            continue
        credit_count = db.query(func.count(SongCredit.id)).filter(SongCredit.artist_id == artist.id).scalar() or 0
        if credit_count and artist.apple_catalog_status == "no_match":
            feature_only_no_match.append(
                {
                    "artist_id": artist.id,
                    "name": artist.name,
                    "credit_count": credit_count,
                    "status": artist.apple_catalog_status,
                }
            )
        elif credit_count and artist.internet_synced_at is None:
            feature_only_unchecked.append(
                {
                    "artist_id": artist.id,
                    "name": artist.name,
                    "credit_count": credit_count,
                    "status": artist.apple_catalog_status or "not_checked",
                }
            )
        if len(feature_only_unchecked) >= 75 and len(feature_only_no_match) >= 75:
            break

    suspicious_feature_credits = []
    for credit in (
        db.query(SongCredit)
        .join(Artist, Artist.id == SongCredit.artist_id)
        .join(Song, Song.id == SongCredit.song_id)
        .filter(SongCredit.role == "featured")
        .order_by(SongCredit.id.desc())
        .limit(1000)
        .all()
    ):
        artist = db.get(Artist, credit.artist_id)
        song = db.get(Song, credit.song_id)
        if artist is None or song is None or _artist_name_in_title(artist.name, song.title):
            continue
        suspicious_feature_credits.append(
            {
                "credit_id": credit.id,
                "artist_id": artist.id,
                "artist_name": artist.name,
                "song_id": song.id,
                "song_title": song.title,
                "album_title": song.album.title if song.album else "",
                "album_artist": song.album.artist.name if song.album and song.album.artist else "",
            }
        )
        if len(suspicious_feature_credits) >= 75:
            break

    solo_looking_collabs = []
    for artist in db.query(Artist).filter(Artist.kind == "collab").order_by(Artist.name.asc()).all():
        if re.search(r"\s(&|\+|x|and|with)\s|,", artist.name, re.IGNORECASE):
            continue
        child_count = (
            db.query(func.count(ArtistMembership.id))
            .filter(ArtistMembership.artist_id == artist.id, ArtistMembership.child_artist_id.isnot(None))
            .scalar()
            or 0
        )
        solo_looking_collabs.append(
            {
                "artist_id": artist.id,
                "name": artist.name,
                "child_count": child_count,
            }
        )
        if len(solo_looking_collabs) >= 75:
            break

    return {
        "impossible_counts": impossible_counts,
        "leading_conjunction_artists": leading_conjunction_artists,
        "feature_only_unchecked": feature_only_unchecked[:75],
        "feature_only_no_match": feature_only_no_match[:75],
        "suspicious_feature_credits": suspicious_feature_credits,
        "solo_looking_collabs": solo_looking_collabs,
    }


@app.get("/data-issues", response_class=HTMLResponse)
def data_issues_page(request: Request, refresh: int = 0, db: Session = Depends(get_session)):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    if refresh:
        invalidate_artist_scores_cache()
    report = _data_issue_report(db)
    response = templates.TemplateResponse(request, "data_issues.html", {"report": report})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/today", response_class=HTMLResponse)
def today_page(request: Request, db: Session = Depends(get_session)):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)

    comparison_count = db.query(func.count(Comparison.id)).scalar() or 0
    start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = (
        db.query(func.count(Comparison.id))
        .filter(Comparison.created_at >= start_of_day)
        .scalar()
        or 0
    )
    compared_songs = db.query(func.count(Song.id)).filter(Song.comparison_count > 0).scalar() or 0
    placement_pending = db.query(func.count(Song.id)).filter(Song.placement_pending == True).scalar() or 0  # noqa: E712
    latest_export = _latest_comparison_export_info()
    snapshot_count = len(list(BACKUP_DIR.glob("*.db"))) if BACKUP_DIR.exists() else 0
    export_warning = ""
    export_count = latest_export.get("count")
    if export_count is None:
        export_warning = "No readable comparison export found yet. Export before imports, restores, or deploy-risky work."
    elif export_count < comparison_count:
        export_warning = f"Latest export has {export_count} comparisons, but the current DB has {comparison_count}. Export before touching data."
    elif export_count > comparison_count:
        export_warning = f"Latest export has {export_count} comparisons, but the current DB has {comparison_count}. Do not overwrite richer data with this DB."

    why_note = (
        db.query(Note)
        .filter(
            Note.target_type == "general",
            func.lower(Note.title) == "why i am doing this project",
        )
        .order_by(Note.id.desc())
        .first()
    )

    cleanup = {
        "unresolved_artists": (
            db.query(func.count(Artist.id))
            .filter((Artist.prompt_resolved == False) | (Artist.prompt_resolved.is_(None)))  # noqa: E712
            .scalar()
            or 0
        ),
        "missing_origin": (
            db.query(func.count(Artist.id))
            .filter(Artist.name != "Various Artists")
            .filter(Artist.country.is_(None), Artist.origin_city.is_(None))
            .scalar()
            or 0
        ),
        "artists_missing_image": (
            db.query(func.count(Artist.id))
            .filter(Artist.image_path.is_(None), Artist.image_url.is_(None))
            .scalar()
            or 0
        ),
        "albums_missing_cover": (
            db.query(func.count(Album.id))
            .filter(Album.cover_path.is_(None), Album.cover_url.is_(None))
            .scalar()
            or 0
        ),
        "album_queue_count": len(_album_confirmation_candidates(db, limit=500)),
    }
    listen_next = _listen_queue_preview(db)
    safety_action = {
        "kind": "export" if export_warning else "snapshot",
        "label": "Export comparisons now" if export_warning else "Snapshot DB before edits",
        "note": export_warning or "Comparison export matches the DB; take a fresh DB snapshot before deeper cleanup.",
    }
    review_songs = loved_songs_needing_review(db)[:5]
    review_albums = cached_loved_albums_needing_review(db)[:5]
    review_prompt = None
    if review_songs:
        s = review_songs[0]
        review_prompt = {"kind": "song", "id": s["id"], "label": f'{s["title"]} - {s["artist"]}'}
    elif review_albums:
        a = review_albums[0]
        review_prompt = {"kind": "album", "id": a["id"], "label": f'{a["title"]} - {a["artist"]}'}
    next_album = _next_album_confirmation(db)
    next_actions = {
        "review": _review_action_from_candidate(review_prompt),
        "artist": _next_unresolved_artist(db),
        "album": next_album,
        "listen": listen_next[0] if listen_next else None,
        "safety": safety_action,
    }

    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "safety": {
                "comparison_count": comparison_count,
                "snapshot_count": snapshot_count,
                "latest_export_name": latest_export.get("name"),
                "latest_export_count": export_count,
                "export_warning": export_warning,
            },
            "ranking": {
                "today_count": today_count,
                "progress": cached_progress_metrics(db),
                "compared_songs": compared_songs,
                "placement_pending": placement_pending,
            },
            "writing": {
                "published_count": db.query(func.count(Note.id)).filter(Note.status == "published").scalar() or 0,
                "draft_count": db.query(func.count(Note.id)).filter(Note.status == "draft").scalar() or 0,
                "subscriber_count": db.query(func.count(Note.id)).filter(Note.visibility == "subscribers").scalar() or 0,
                "why_note": why_note,
                "review_songs": review_songs,
                "review_albums": review_albums,
            },
            "cleanup": cleanup,
            "listen_next": listen_next,
            "next_actions": next_actions,
        },
    )


@app.get("/playlists/{playlist_id}", response_class=HTMLResponse)
def playlist_detail(playlist_id: int, request: Request, db: Session = Depends(get_session)):
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        return HTMLResponse("Playlist not found", status_code=404)

    rows = (
        db.query(PlaylistSong)
        .options(joinedload(PlaylistSong.song).joinedload(Song.album).joinedload(Album.artist))
        .filter(PlaylistSong.playlist_id == playlist_id)
        .all()
    )
    return templates.TemplateResponse(
        request, "playlist.html", {"playlist": playlist, "rows": rows}
    )


@app.get("/songs", response_class=HTMLResponse)
def songs_list(
    request: Request,
    q: str | None = Query(None),
    tier: str | None = Query(None),
    limit: int = 200,
    db: Session = Depends(get_session),
):
    query = (
        db.query(Song)
        .options(joinedload(Song.album).joinedload(Album.artist))
    )
    if q:
        like = f"%{q}%"
        filters = (
            (Song.title.ilike(like))
            | (Album.title.ilike(like))
            | (Artist.name.ilike(like))
            | (Album.genre.ilike(like))
        )
        if q.isdigit():
            filters = filters | (Album.year == int(q))
        query = query.join(Song.album).join(Album.artist).filter(filters)
    songs = query.order_by(Song.glicko_rating.desc()).limit(1000).all()
    parsed_tier = int(tier) if tier and tier.isdigit() else None
    songs_with_stars = [(s, myk_score(s.glicko_rating, s.glicko_rd)) for s in songs]
    if parsed_tier is not None:
        songs_with_stars = [(s, st) for (s, st) in songs_with_stars if st == parsed_tier]
    songs_with_stars = songs_with_stars[:limit]
    reviewed_song_ids = {
        tid for (tid,) in db.query(Note.target_id).filter(Note.target_type == "song", Note.target_id.isnot(None)).distinct().all()
    }
    return templates.TemplateResponse(
        request, "songs.html",
        {"songs_with_stars": songs_with_stars, "q": q or "", "limit": limit, "tier": parsed_tier, "reviewed_ids": reviewed_song_ids, "render_myks": render_myks},
    )


@app.get("/albums", response_class=HTMLResponse)
def albums_page(request: Request, unknown_first: int = 0, all: int = 0, refresh: int = 0, db: Session = Depends(get_session)):
    reviewed = {
        tid for (tid,) in db.query(Note.target_id).filter(Note.target_type == "album", Note.target_id.isnot(None)).distinct().all()
    }
    covers = {aid: cp for (aid, cp) in db.query(Album.id, Album.cover_path).filter(Album.cover_path.isnot(None)).all()}
    all_albums = cached_album_scores(db, refresh=bool(refresh))
    albums_main = [a for a in all_albums if a.release_type == "album"]
    albums_eps = [a for a in all_albums if a.release_type == "ep"]
    if request.state.is_admin and unknown_first:
        albums_main.sort(key=lambda a: (a.displayed_total_tracks is not None, -a.score, a.title.lower()))
        albums_eps.sort(key=lambda a: (a.displayed_total_tracks is not None, -a.score, a.title.lower()))
    visible_albums = albums_main if all else albums_main[:250]
    visible_eps = albums_eps if all else albums_eps[:100]
    return templates.TemplateResponse(
        request,
        "albums.html",
        {
            "albums": visible_albums,
            "eps": visible_eps,
            "album_total_count": len(albums_main),
            "ep_total_count": len(albums_eps),
            "album_showing_all": bool(all),
            "reviewed_ids": reviewed,
            "covers": covers,
            "unknown_first": bool(unknown_first),
            "myk_score": myk_score,
            "render_myks": render_myks,
        },
    )


@app.get("/artists", response_class=HTMLResponse)
def artists_page(request: Request, refresh: int = 0, all: int = 0, db: Session = Depends(get_session)):
    reviewed = {
        tid for (tid,) in db.query(Note.target_id).filter(Note.target_type == "artist", Note.target_id.isnot(None)).distinct().all()
    }
    images = {aid: ip for (aid, ip) in db.query(Artist.id, Artist.image_path).filter(Artist.image_path.isnot(None)).all()}
    apple_synced = {
        aid
        for (aid,) in db.query(Artist.id).filter(Artist.internet_synced_at.isnot(None)).all()
    }
    rows = cached_artist_scores(db, refresh=bool(refresh))
    visible_rows = rows if all else rows[:250]
    response = templates.TemplateResponse(
        request,
        "artists.html",
        {
            "artists": visible_rows,
            "artist_total_count": len(rows),
            "artist_showing_all": bool(all),
            "reviewed_ids": reviewed,
            "images": images,
            "apple_synced": apple_synced,
            "myk_score": myk_score,
            "render_myks": render_myks,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/api/artist-search")
def api_artist_search(q: str, db: Session = Depends(get_session)):
    if not q or len(q) < 1:
        return {"results": []}
    like = f"%{q}%"
    rows = (
        db.query(Artist)
        .filter(Artist.name.ilike(like))
        .order_by(Artist.name.asc())
        .limit(20)
        .all()
    )
    return {"results": [{"id": a.id, "name": a.name} for a in rows]}


@app.get("/api/song-search")
def api_song_search(q: str, db: Session = Depends(get_session)):
    if not q or len(q) < 2:
        return {"results": []}
    like = f"%{q}%"
    songs = (
        db.query(Song)
        .join(Song.album)
        .join(Album.artist)
        .options(joinedload(Song.album).joinedload(Album.artist))
        .filter((Song.title.ilike(like)) | (Album.title.ilike(like)) | (Artist.name.ilike(like)))
        .order_by(Song.title.asc())
        .limit(20)
        .all()
    )
    return {
        "results": [
            {
                "id": s.id,
                "title": s.title,
                "artist": s.album.artist.name if s.album and s.album.artist else "",
                "album": s.album.title if s.album else "",
            }
            for s in songs
        ]
    }


def _notes_for(db: Session, request: Request, target_type: str, target_id: int) -> list[dict]:
    query = db.query(Note).filter(Note.target_type == target_type, Note.target_id == target_id)
    if not is_admin(request):
        query = query.filter(Note.status == "published")
    notes = query.order_by(Note.created_at.desc()).all()
    out = []
    unlocked = is_subscriber(request, db)
    for n in notes:
        locked = (n.visibility == "subscribers") and not unlocked
        comments = (
            db.query(Comment)
            .filter(Comment.note_id == n.id, Comment.approved == True)  # noqa: E712
            .order_by(Comment.created_at.asc())
            .all()
        )
        out.append({
            "id": n.id,
            "title": n.title,
            "kind": n.kind or "essay",
            "status": n.status or "published",
            "body_html": "" if locked else render_markdown(n.body),
            "locked": locked,
            "teaser": _teaser(n.body) if locked else "",
            "created_at": n.created_at,
            "related_songs": related_songs_for_note(db, n.id),
            "comments": [
                {"id": c.id, "author_name": c.author_name, "body": c.body, "created_at": c.created_at}
                for c in comments
            ],
            "pending_comments": [
                {"id": c.id, "author_name": c.author_name, "body": c.body, "created_at": c.created_at}
                for c in (
                    db.query(Comment)
                    .filter(Comment.note_id == n.id, Comment.approved == False)  # noqa: E712
                    .order_by(Comment.created_at.asc())
                    .all()
                )
            ] if is_admin(request) else [],
        })
    return out


@app.get("/songs/{song_id}", response_class=HTMLResponse)
def song_detail(song_id: int, request: Request, db: Session = Depends(get_session)):
    s = (
        db.query(Song)
        .options(joinedload(Song.album).joinedload(Album.artist))
        .filter(Song.id == song_id)
        .first()
    )
    if s is None:
        return HTMLResponse("Song not found", status_code=404)
    playlists = (
        db.query(Playlist)
        .join(PlaylistSong, PlaylistSong.playlist_id == Playlist.id)
        .filter(PlaylistSong.song_id == song_id)
        .all()
    )
    linked = _song_link_map(db, {song_id}).get(song_id, [])
    return templates.TemplateResponse(
        request, "song_detail.html",
        {
            "song": s,
            "stars": myk_score(s.glicko_rating, s.glicko_rd),
            "render_myks": render_myks,
            "playlists": playlists,
            "linked_songs": linked,
            "external_links": _song_external_links(s),
            "notes": _notes_for(db, request, "song", song_id),
        },
    )


@app.get("/albums/{album_id}", response_class=HTMLResponse)
def album_detail(album_id: int, request: Request, db: Session = Depends(get_session)):
    al = (
        db.query(Album)
        .options(joinedload(Album.artist), joinedload(Album.songs), joinedload(Album.tracks))
        .filter(Album.id == album_id)
        .first()
    )
    if al is None:
        return HTMLResponse("Album not found", status_code=404)
    album_summary = album_score_for(db, al)
    return templates.TemplateResponse(
        request, "album_detail.html",
        {
            "album": al,
            "album_myks": myk_score(album_summary.score) if album_summary else None,
            "effective_total_tracks": effective_album_total_tracks(al),
            "track_rows": _album_track_rows(al),
            "star_tier": myk_tier,
            "myk_score": myk_score,
            "render_myks": render_myks,
            "is_rankable_album": is_rankable_album,
            "notes": _notes_for(db, request, "album", album_id),
        },
    )


@app.get("/artists/{artist_id}", response_class=HTMLResponse)
def artist_detail(artist_id: int, request: Request, db: Session = Depends(get_session)):
    ar = db.get(Artist, artist_id)
    if ar is None:
        return HTMLResponse("Artist not found", status_code=404)
    albums_sorted = sorted(ar.albums, key=lambda a: (-(a.year or 0), a.title.lower()))
    artist_summary = artist_score_for(db, ar)
    listened_album_ids = _listened_album_ids(db)
    local_albums_by_rg = {
        al.release_group_mb_id: al
        for al in ar.albums
        if al.release_group_mb_id
    }
    release_rows = []
    if ar.releases:
        for rel in sorted(ar.releases, key=lambda r: (-(r.year or 0), r.title.lower())):
            local_album = local_albums_by_rg.get(rel.release_group_mb_id)
            listened = bool(local_album and local_album.id in listened_album_ids)
            release_rows.append(
                {
                    "title": rel.title,
                    "year": rel.year,
                    "primary_type": rel.primary_type or "",
                    "track_count": rel.track_count,
                    "listened": listened,
                    "href": f"/albums/{local_album.id}" if local_album else None,
                }
            )
    else:
        for al in albums_sorted:
            release_rows.append(
                {
                    "title": al.title,
                    "year": al.year,
                    "primary_type": classify_release_type(al),
                    "track_count": effective_album_total_tracks(al),
                    "listened": al.id in listened_album_ids,
                    "href": f"/albums/{al.id}",
                }
            )

    # Memberships
    memberships = db.query(ArtistMembership).filter(ArtistMembership.artist_id == artist_id).all()
    member_rows = []
    person_ids_here: set[int] = set()
    for m in memberships:
        if m.person_id is not None:
            p = db.get(Person, m.person_id)
            if p is not None:
                person_ids_here.add(p.id)
                member_rows.append({
                    "id": m.id, "kind": "person", "name": p.name,
                    "gender": p.gender, "role": m.role, "link": None,
                })
        elif m.child_artist_id is not None:
            ca = db.get(Artist, m.child_artist_id)
            if ca is not None:
                member_rows.append({
                    "id": m.id, "kind": "act", "name": ca.name,
                    "gender": None, "role": m.role,
                    "link": f"/artists/{ca.id}",
                })

    # Related acts: any other artist sharing at least one person with this artist
    related = []
    if person_ids_here:
        related_artist_ids = {
            aid for (aid,) in db.query(ArtistMembership.artist_id)
            .filter(
                ArtistMembership.person_id.in_(person_ids_here),
                ArtistMembership.artist_id != artist_id,
            ).distinct().all()
        }
        for rid in related_artist_ids:
            ra = db.get(Artist, rid)
            if ra is not None:
                related.append({"id": ra.id, "name": ra.name})
        related.sort(key=lambda r: r["name"])

    return templates.TemplateResponse(
        request, "artist_detail.html",
        {
            "artist": ar,
            "artist_summary": artist_summary,
            "myk_score": myk_score,
            "render_myks": render_myks,
            "albums": albums_sorted,
            "release_rows": release_rows,
            "notes": _notes_for(db, request, "artist", artist_id),
            "members": member_rows,
            "related_acts": related,
        },
    )


@app.get("/album-queue", response_class=HTMLResponse)
def album_queue_page(request: Request, db: Session = Depends(get_session)):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    rows = _album_confirmation_candidates(db)
    items = []
    for album in rows:
        items.append(
            {
                "id": album.id,
                "title": album.title,
                "artist_name": album.artist.name if album.artist else "",
                "year": album.year,
                "song_count": len(album.songs),
            }
        )
    return templates.TemplateResponse(request, "album_queue.html", {"items": items})


@app.get("/listen-next", response_class=HTMLResponse)
def listen_next_page(request: Request, db: Session = Depends(get_session)):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    rows = db.query(ListenQueueItem).order_by(ListenQueueItem.created_at.desc()).all()
    items = []
    for row in rows:
        label = "Unknown"
        href = "#"
        subtitle = ""
        if row.target_type == "album":
            album = db.get(Album, row.target_id)
            if album:
                label = album.title
                href = f"/albums/{album.id}"
                subtitle = album.artist.name if album.artist else ""
        elif row.target_type == "artist":
            artist = db.get(Artist, row.target_id)
            if artist:
                label = artist.name
                href = f"/artists/{artist.id}"
        items.append(
            {
                "id": row.id,
                "target_type": row.target_type,
                "label": label,
                "href": href,
                "subtitle": subtitle,
                "note": row.note or "",
                "created_at": row.created_at,
            }
        )
    return templates.TemplateResponse(request, "listen_next.html", {"items": items})


@app.get("/library-import", response_class=HTMLResponse)
def library_import_page(request: Request):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "library_import.html", {"status": LIBRARY_IMPORT_STATUS})


@app.get("/apple-music", response_class=HTMLResponse)
def apple_music_page(request: Request):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    config = get_apple_music_config()
    return templates.TemplateResponse(
        request,
        "apple_music.html",
        {
            "configured": config.configured,
            "missing": config.missing,
            "team_id_set": bool(config.team_id),
            "key_id_set": bool(config.key_id),
            "private_key_set": bool(config.private_key),
        },
    )


@app.post("/api/apple-music/developer-token")
def api_apple_music_developer_token(request: Request):
    require_admin(request)
    try:
        origin = f"{request.url.scheme}://{request.url.netloc}"
        token = generate_developer_token(ttl_seconds=3600, origins=[origin])
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "token": token, "ttl_seconds": 3600, "origin": origin}


def _run_library_import(xml_path: Path) -> None:
    from .importer import import_library

    try:
        stats = import_library(xml_path)
        LIBRARY_IMPORT_STATUS.update(
            {
                "running": False,
                "done": True,
                "error": None,
                "stats": stats,
                "finished_at": datetime.utcnow().isoformat(),
            }
        )
    except Exception as exc:
        LIBRARY_IMPORT_STATUS.update(
            {
                "running": False,
                "done": False,
                "error": str(exc),
                "finished_at": datetime.utcnow().isoformat(),
            }
        )


@app.post("/api/library/import")
async def api_library_import(request: Request):
    require_admin(request)
    if LIBRARY_IMPORT_STATUS.get("running"):
        return JSONResponse({"ok": False, "reason": "already_running"}, status_code=409)

    target = data_dir() / "Library.xml"
    tmp = data_dir() / "Library.xml.uploading"
    total = 0
    with tmp.open("wb") as handle:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            handle.write(chunk)
    if total == 0:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise HTTPException(400, "empty upload")
    tmp.replace(target)
    LIBRARY_IMPORT_STATUS.update(
        {
            "running": True,
            "done": False,
            "error": None,
            "stats": None,
            "filename": target.name,
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
        }
    )
    threading.Thread(target=_run_library_import, args=(target,), daemon=True).start()
    return JSONResponse({"ok": True, "bytes": total}, status_code=202)


@app.get("/api/library/import-status")
def api_library_import_status(request: Request):
    require_admin(request)
    return LIBRARY_IMPORT_STATUS


class AlbumDecisionBody(BaseModel):
    listened: bool


class AlbumMetaBody(BaseModel):
    total_track_count: int | None = None


class ListenQueueBody(BaseModel):
    target_type: str
    target_id: int
    note: str | None = None


class AppleMusicPreviewTrack(BaseModel):
    id: str | None = None
    name: str | None = None
    artistName: str | None = None
    albumName: str | None = None
    genreName: str | None = None
    releaseDate: str | None = None
    durationInMillis: int | None = None
    trackNumber: int | None = None


class AppleMusicPreviewPlaylist(BaseModel):
    id: str
    name: str
    tracks: list[AppleMusicPreviewTrack] = []


class AppleMusicPreviewBody(BaseModel):
    playlists: list[AppleMusicPreviewPlaylist] = []


class AppleCatalogBatchBody(BaseModel):
    artist_ids: list[int] = []
    force: bool = False


class AppleCatalogManualBody(BaseModel):
    catalog_artist_id: str


class AppleCatalogStatusBody(BaseModel):
    status: str


def _apple_track_match_key(track: AppleMusicPreviewTrack) -> tuple[str, str, str]:
    return (
        (track.name or "").strip().lower(),
        (track.albumName or "").strip().lower(),
        (track.artistName or "").strip().lower(),
    )


def _apple_year(release_date: str | None) -> int | None:
    if not release_date:
        return None
    try:
        return int(str(release_date)[:4])
    except Exception:
        return None


def _apple_album_family_title(title: str | None) -> str:
    value = (title or "").lower().strip()
    value = re.sub(r"\s*\((deluxe|expanded|bonus|remastered|anniversary|.*anniversary).*?\)\s*$", "", value)
    value = re.sub(r"\s*-\s*(deluxe|expanded|bonus|remastered|anniversary|.*anniversary).*?$", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _is_apple_countable_release(album: dict, artist_name: str) -> bool:
    attrs = album.get("attributes") or {}
    name = attrs.get("name") or ""
    if attrs.get("isSingle"):
        return False
    if attrs.get("isCompilation"):
        return False
    if int(attrs.get("trackCount") or 0) <= 3:
        return False
    lowered = name.lower()
    if " single" in lowered or lowered.endswith(" single"):
        return False
    if "anniversary" in lowered:
        return False
    album_artist = (attrs.get("artistName") or "").lower()
    if artist_name and artist_name.lower() not in album_artist:
        return False
    return True


def _apply_apple_catalog_artist_totals(db: Session, artist: Artist, catalog_artist: dict, albums: list[dict]) -> dict:
    countable = []
    seen_titles: set[str] = set()
    for album in albums:
        if not _is_apple_countable_release(album, artist.name):
            continue
        attrs = album.get("attributes") or {}
        family = _apple_album_family_title(attrs.get("name"))
        if not family or family in seen_titles:
            continue
        seen_titles.add(family)
        countable.append(album)

    existing_release_rows = {
        row.release_group_mb_id: row
        for row in db.query(ArtistRelease).filter(ArtistRelease.artist_id == artist.id).all()
    }
    seen_release_ids: set[str] = set()
    track_total = 0
    for album in countable:
        attrs = album.get("attributes") or {}
        release_id = f"apple:{album.get('id')}"
        seen_release_ids.add(release_id)
        track_count = int(attrs.get("trackCount") or 0)
        track_total += track_count
        release_row = existing_release_rows.get(release_id)
        if release_row is None:
            release_row = ArtistRelease(
                artist_id=artist.id,
                release_group_mb_id=release_id,
                title=attrs.get("name") or "Untitled",
                year=_apple_year(attrs.get("releaseDate")),
                primary_type="album",
                track_count=track_count,
            )
            db.add(release_row)
        else:
            release_row.title = attrs.get("name") or release_row.title
            release_row.year = release_row.year or _apple_year(attrs.get("releaseDate"))
            release_row.primary_type = "album"
            release_row.track_count = track_count or release_row.track_count

    artist.internet_release_total = len(countable)
    artist.internet_track_total = track_total
    artist.internet_synced_at = datetime.utcnow()
    artist.apple_catalog_id = str(catalog_artist.get("id") or "")
    artist.apple_catalog_status = "matched"
    db.commit()
    invalidate_artist_scores_cache()
    return {
        "catalog_artist_id": catalog_artist.get("id"),
        "catalog_artist_name": (catalog_artist.get("attributes") or {}).get("name"),
        "release_total": artist.internet_release_total,
        "track_total": artist.internet_track_total,
        "releases": [
            {
                "id": album.get("id"),
                "name": (album.get("attributes") or {}).get("name"),
                "releaseDate": (album.get("attributes") or {}).get("releaseDate"),
                "trackCount": (album.get("attributes") or {}).get("trackCount"),
            }
            for album in countable
        ],
    }


def _apple_catalog_enrich_artist(db: Session, artist: Artist, storefront: str = "us", catalog_artist_id: str | None = None) -> dict:
    if is_various_artists_name(artist.name):
        return {"ok": False, "artist_id": artist.id, "artist_name": artist.name, "reason": "skip_various_artists"}

    from .apple_music import catalog_artist_albums, get_catalog_artist, search_catalog_artists

    if catalog_artist_id:
        catalog_artist = get_catalog_artist(catalog_artist_id, storefront=storefront)
        if not catalog_artist:
            return {"ok": False, "artist_id": artist.id, "artist_name": artist.name, "reason": "catalog_artist_id_not_found"}
    else:
        hits = search_catalog_artists(artist.name, storefront=storefront, limit=5)
        if not hits:
            artist.internet_synced_at = datetime.utcnow()
            artist.apple_catalog_status = "no_match"
            db.commit()
            invalidate_artist_scores_cache()
            return {"ok": False, "artist_id": artist.id, "artist_name": artist.name, "reason": "no_catalog_artist_match"}
        catalog_artist = hits[0]
    if not catalog_artist:
        return {"ok": False, "artist_id": artist.id, "artist_name": artist.name, "reason": "no_catalog_artist_match"}
    albums = catalog_artist_albums(catalog_artist["id"], storefront=storefront)
    result = _apply_apple_catalog_artist_totals(db, artist, catalog_artist, albums)
    if catalog_artist_id:
        artist.apple_catalog_status = "manual"
        db.commit()
        invalidate_artist_scores_cache()
        result["status"] = "manual"
    return {"ok": True, "artist_id": artist.id, "artist_name": artist.name, **result}


def _import_apple_music_playlists(db: Session, playlists: list[AppleMusicPreviewPlaylist]) -> dict:
    from .importer import (
        ensure_collab_artist,
        ensure_song_credit,
        get_or_create_album,
        get_or_create_artist,
        parse_featured_artists,
        parse_playlist_name,
    )
    from .artist_names import split_collaboration_artists
    from .dedupe import merge_case_duplicates, merge_known_artist_aliases
    from .genres import normalize_genre
    from .scoring import is_various_artists_name

    stats = {
        "playlists_seen": 0,
        "playlists_created": 0,
        "songs_created": 0,
        "songs_updated": 0,
        "playlist_songs_created": 0,
        "artists_after": 0,
        "albums_after": 0,
    }
    artist_cache: dict[str, int] = {}
    known_artist_names: set[str] = {name for (name,) in db.query(Artist.name).all()}

    for playlist in playlists:
        month, year = parse_playlist_name(playlist.name)
        if month is None:
            continue
        stats["playlists_seen"] += 1
        row = db.query(Playlist).filter(Playlist.name == playlist.name).one_or_none()
        if row is None:
            row = Playlist(
                name=playlist.name,
                month=month,
                year=year,
                apple_library_id=playlist.id,
            )
            db.add(row)
            db.flush()
            stats["playlists_created"] += 1
        elif not row.apple_library_id:
            row.apple_library_id = playlist.id

        seen_song_pks: set[int] = {
            song_id for (song_id,) in db.query(PlaylistSong.song_id).filter_by(playlist_id=row.id).all()
        }
        for track in playlist.tracks:
            if not track.name:
                continue
            artist_name = (track.artistName or "Unknown Artist").strip()
            album_title = (track.albumName or "Unknown Album").strip()
            cache_key = artist_name
            if cache_key in artist_cache:
                artist = db.get(Artist, artist_cache[cache_key])
            else:
                artist = get_or_create_artist(db, artist_name)
                artist_cache[cache_key] = artist.id
                known_artist_names.add(artist.name)
            album = get_or_create_album(
                db,
                artist,
                album_title,
                _apple_year(track.releaseDate),
                normalize_genre(track.genreName),
            )
            song = None
            if track.id:
                song = (
                    db.query(Song)
                    .filter((Song.apple_library_id == track.id) | (Song.apple_track_id == track.id))
                    .order_by(Song.id.asc())
                    .first()
                )
            if song is None:
                song = (
                    db.query(Song)
                    .filter(Song.album_id == album.id, Song.title.ilike(track.name))
                    .order_by(Song.id.asc())
                    .first()
                )
            if song is None:
                song = Song(
                    album_id=album.id,
                    title=track.name,
                    track_number=track.trackNumber,
                    duration_ms=track.durationInMillis,
                    apple_library_id=track.id,
                    liked=True,
                )
                db.add(song)
                db.flush()
                stats["songs_created"] += 1
            else:
                changed = False
                if track.id and not song.apple_library_id:
                    song.apple_library_id = track.id
                    changed = True
                if song.track_number is None and track.trackNumber is not None:
                    song.track_number = track.trackNumber
                    changed = True
                if song.duration_ms is None and track.durationInMillis is not None:
                    song.duration_ms = track.durationInMillis
                    changed = True
                if not song.liked:
                    song.liked = True
                    changed = True
                if changed:
                    stats["songs_updated"] += 1

            primary_names = split_collaboration_artists(
                artist_name,
                known_names=known_artist_names,
                require_known_part=True,
            )
            if len(primary_names) > 1:
                primary_artists = []
                for primary_name in primary_names:
                    primary_artist = get_or_create_artist(db, primary_name)
                    known_artist_names.add(primary_artist.name)
                    primary_artists.append(primary_artist)
                    ensure_song_credit(db, song, primary_artist, "primary")
                if not is_various_artists_name(artist.name):
                    ensure_collab_artist(db, artist, primary_artists)
            else:
                ensure_song_credit(db, song, artist, "primary")
            for featured_name in parse_featured_artists(track.name):
                feat_artist = get_or_create_artist(db, featured_name)
                known_artist_names.add(feat_artist.name)
                ensure_song_credit(db, song, feat_artist, "featured")

            if song.id not in seen_song_pks:
                db.add(PlaylistSong(playlist_id=row.id, song_id=song.id))
                seen_song_pks.add(song.id)
                stats["playlist_songs_created"] += 1

    db.commit()
    merge_case_duplicates(db)
    merge_known_artist_aliases(db)
    stats["artists_after"] = db.query(Artist).count()
    stats["albums_after"] = db.query(Album).count()
    invalidate_artist_scores_cache()
    return stats


@app.post("/api/apple-music/sync-preview")
def api_apple_music_sync_preview(body: AppleMusicPreviewBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    month_playlists = []
    total_tracks = 0
    unique_apple_ids: set[str] = set()
    unique_match_keys: set[tuple[str, str, str]] = set()
    apple_ids = {track.id for playlist in body.playlists for track in playlist.tracks if track.id}
    existing_by_apple_id = set()
    if apple_ids:
        existing_by_apple_id = {
            value
            for (value,) in db.query(Song.apple_track_id).filter(Song.apple_track_id.in_(apple_ids)).all()
            if value
        }

    db_song_rows = (
        db.query(Song.title, Album.title, Artist.name)
        .join(Album, Song.album_id == Album.id)
        .join(Artist, Album.artist_id == Artist.id)
        .all()
    )
    db_match_keys = {
        (
            (title or "").strip().lower(),
            (album_title or "").strip().lower(),
            (artist_name or "").strip().lower(),
        )
        for title, album_title, artist_name in db_song_rows
    }
    db_playlist_counts = {
        name: count
        for name, count in (
            db.query(Playlist.name, func.count(PlaylistSong.id))
            .outerjoin(PlaylistSong, PlaylistSong.playlist_id == Playlist.id)
            .group_by(Playlist.id)
            .all()
        )
    }

    for playlist in body.playlists:
        track_count = len(playlist.tracks)
        total_tracks += track_count
        matched_by_apple_id = 0
        matched_by_metadata = 0
        missing_tracks = []
        for track in playlist.tracks:
            if track.id:
                unique_apple_ids.add(track.id)
                if track.id in existing_by_apple_id:
                    matched_by_apple_id += 1
            key = _apple_track_match_key(track)
            if any(key):
                unique_match_keys.add(key)
                if key in db_match_keys:
                    matched_by_metadata += 1
            if track.id not in existing_by_apple_id and key not in db_match_keys and len(missing_tracks) < 8:
                missing_tracks.append(
                    {
                        "id": track.id,
                        "name": track.name,
                        "artistName": track.artistName,
                        "albumName": track.albumName,
                    }
                )
        db_track_count = db_playlist_counts.get(playlist.name)
        month_playlists.append(
            {
                "id": playlist.id,
                "name": playlist.name,
                "apple_track_count": track_count,
                "db_track_count": db_track_count,
                "delta": None if db_track_count is None else track_count - int(db_track_count),
                "matched_by_apple_id": matched_by_apple_id,
                "matched_by_metadata": matched_by_metadata,
                "missing_sample": missing_tracks,
            }
        )

    month_playlists.sort(key=lambda row: row["name"])
    return {
        "ok": True,
        "playlist_count": len(body.playlists),
        "total_playlist_tracks": total_tracks,
        "unique_apple_ids": len(unique_apple_ids),
        "unique_metadata_tracks": len(unique_match_keys),
        "existing_by_apple_id": len(existing_by_apple_id),
        "existing_by_metadata": len(unique_match_keys & db_match_keys),
        "playlists": month_playlists,
    }


@app.post("/api/apple-music/import")
def api_apple_music_import(body: AppleMusicPreviewBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    try:
        snapshot_path = snapshot_db("pre-apple-music-sync")
        export_path = export_comparisons_from_db(db, "pre-apple-music-sync")
        stats = _import_apple_music_playlists(db, body.playlists)
        return {
            "ok": True,
            "snapshot_path": snapshot_path,
            "export_path": export_path,
            "stats": stats,
            "comparison_count": db.query(func.count(Comparison.id)).scalar() or 0,
        }
    except Exception as exc:
        db.rollback()
        return JSONResponse(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            status_code=500,
        )


@app.post("/api/albums/{album_id}/listened")
def set_album_listened(album_id: int, body: AlbumDecisionBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(404, "album not found")
    album.confirmed_listened = bool(body.listened)
    album.excluded_from_listened = not bool(body.listened)
    db.commit()
    invalidate_artist_scores_cache()
    return {"ok": True}


@app.post("/api/albums/{album_id}/meta")
def set_album_meta(album_id: int, body: AlbumMetaBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(404, "album not found")
    total_track_count = body.total_track_count
    if total_track_count is not None and total_track_count < 1:
        total_track_count = None
    album.total_track_count = total_track_count
    db.commit()
    invalidate_artist_scores_cache()
    return {
        "ok": True,
        "total_track_count": effective_album_total_tracks(album),
        "rankable": is_rankable_album(album),
    }


@app.post("/api/listen-next")
def add_listen_next(body: ListenQueueBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    if body.target_type not in ("album", "artist"):
        raise HTTPException(400, "invalid target")
    existing = (
        db.query(ListenQueueItem)
        .filter(ListenQueueItem.target_type == body.target_type, ListenQueueItem.target_id == body.target_id)
        .first()
    )
    if existing is not None:
        if body.note:
            existing.note = body.note
            db.commit()
        return {"ok": True, "created": False}
    row = ListenQueueItem(target_type=body.target_type, target_id=body.target_id, note=body.note)
    db.add(row)
    db.commit()
    return {"ok": True, "created": True}


@app.delete("/api/listen-next/{item_id}")
def delete_listen_next(item_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    row = db.get(ListenQueueItem, item_id)
    if row is None:
        raise HTTPException(404, "not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


class SongLinkBody(BaseModel):
    other_song_id: int
    relation: str = "same_song"
    notes: str | None = None


@app.post("/api/songs/{song_id}/links")
def add_song_link(song_id: int, body: SongLinkBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    left = db.get(Song, song_id)
    right = db.get(Song, body.other_song_id)
    if left is None or right is None:
        raise HTTPException(404, "song not found")
    if left.id == right.id:
        raise HTTPException(400, "cannot link a song to itself")
    relation = "same_song"
    a_id, b_id = sorted((left.id, right.id))
    existing = (
        db.query(SongLink)
        .filter(SongLink.left_song_id == a_id, SongLink.right_song_id == b_id, SongLink.relation == relation)
        .one_or_none()
    )
    if existing is None:
        db.add(SongLink(left_song_id=a_id, right_song_id=b_id, relation=relation, notes=body.notes))
        db.commit()
    return {"ok": True}


@app.delete("/api/songs/{song_id}/links/{other_song_id}")
def delete_song_link(song_id: int, other_song_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    a_id, b_id = sorted((song_id, other_song_id))
    row = (
        db.query(SongLink)
        .filter(SongLink.left_song_id == a_id, SongLink.right_song_id == b_id, SongLink.relation == "same_song")
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "link not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ---------- Gender / band prompt queue ----------

@app.get("/api/next-artist-prompt")
def next_artist_prompt(exclude: str | None = None, db: Session = Depends(get_session)):
    """Return one artist that still needs classification (kind unset OR no memberships),
    prioritizing artists with songs in playlists."""
    excluded_ids: set[int] = set()
    if exclude:
        for part in exclude.split(","):
            part = part.strip()
            if part.isdigit():
                excluded_ids.add(int(part))
    candidates = (
        db.query(Artist)
        .join(Album, Album.artist_id == Artist.id)
        .join(Song, Song.album_id == Album.id)
        .join(PlaylistSong, PlaylistSong.song_id == Song.id)
        .filter(func.lower(Artist.name) != "various artists")
        .filter((Artist.prompt_resolved.is_(None)) | (Artist.prompt_resolved != True))  # noqa: E712
        .distinct()
        .all()
    )

    def unresolved(a: Artist) -> bool:
        memberships = db.query(ArtistMembership).filter(ArtistMembership.artist_id == a.id).all()
        membership_count = len(memberships)
        child_count = sum(1 for m in memberships if m.child_artist_id is not None)
        person_members = [m for m in memberships if m.person_id is not None]
        name_lower = (a.name or "").lower()
        collab_hint = any(token in name_lower for token in (" & ", ",", " feat.", " featuring ", " ft. ", " x "))
        if a.kind is None:
            return True
        if a.kind == "solo":
            if membership_count > 1 or child_count > 0 or collab_hint:
                return True
            if a.gender is None or a.gender == "Unknown":
                return True
            if a.gender in ("M", "F", "NB"):
                return False
            if person_members:
                person = db.get(Person, person_members[0].person_id)
                if person is None or person.gender == "unknown":
                    return True
        if a.kind in ("group", "collab") and membership_count == 0:
            return True
        return False

    artist = next((a for a in candidates if a.id not in excluded_ids and unresolved(a)), None)
    if artist is None and excluded_ids:
        artist = next((a for a in candidates if unresolved(a)), None)
    if artist is None:
        return JSONResponse({"artist": None}, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    return JSONResponse(
        {"artist": {"id": artist.id, "name": artist.name, "kind": artist.kind}},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


class ArtistMetaBody(BaseModel):
    artist_id: int
    gender: str  # M, F, NB, Band, Unknown


class ArtistOriginBody(BaseModel):
    city: str | None = None
    region: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None


@app.post("/api/artist-meta")
def set_artist_meta(body: ArtistMetaBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    artist = db.get(Artist, body.artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    if body.gender not in ("M", "F", "NB", "Band", "Unknown"):
        raise HTTPException(400, "invalid gender value")
    artist.gender = body.gender
    artist.is_band = body.gender == "Band"
    artist.prompt_resolved = True
    db.commit()
    return {"ok": True}


@app.post("/api/artists/{artist_id}/origin")
def set_artist_origin(artist_id: int, body: ArtistOriginBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    artist.origin_city = (body.city or "").strip() or None
    artist.origin_region = (body.region or "").strip() or None
    artist.country = (body.country or "").strip().upper() or None
    artist.origin_lat = body.lat
    artist.origin_lon = body.lon
    hit = _city_lookup(artist.origin_city, artist.origin_region)
    if hit and (artist.origin_lat is None or artist.origin_lon is None):
        artist.origin_lat = hit["lat"]
        artist.origin_lon = hit["lon"]
    db.commit()
    return {"ok": True}


# ---------- Notes / Blog ----------

@app.get("/notes", response_class=HTMLResponse)
def notes_index(request: Request, db: Session = Depends(get_session)):
    query = db.query(Note)
    if not is_admin(request):
        query = query.filter(Note.status == "published")
    notes = query.order_by(Note.created_at.desc()).all()
    unlocked = is_subscriber(request, db)
    items = []
    for n in notes:
        locked = (n.visibility == "subscribers") and not unlocked
        comments = (
            db.query(Comment)
            .filter(Comment.note_id == n.id, Comment.approved == True)  # noqa: E712
            .order_by(Comment.created_at.asc())
            .all()
        )
        items.append({
            "id": n.id,
            "title": n.title or "",
            "body_html": "" if locked else render_markdown(n.body),
            "locked": locked,
            "kind": n.kind or "essay",
            "status": n.status or "published",
            "teaser": _teaser(n.body) if locked else "",
            "created_at": n.created_at,
            "updated_at": n.updated_at,
            "target": resolve_target(db, n.target_type, n.target_id),
            "related_songs": related_songs_for_note(db, n.id),
            "comment_count": len(comments),
            "comments": [
                {"id": c.id, "author_name": c.author_name, "body": c.body, "created_at": c.created_at}
                for c in comments
            ],
            "pending_comments": [
                {"id": c.id, "author_name": c.author_name, "body": c.body, "created_at": c.created_at}
                for c in (
                    db.query(Comment)
                    .filter(Comment.note_id == n.id, Comment.approved == False)  # noqa: E712
                    .order_by(Comment.created_at.asc())
                    .all()
                )
            ] if is_admin(request) else [],
        })
    return templates.TemplateResponse(
        request, "notes_index.html",
        {"items": items, "is_subscriber": unlocked},
    )


@app.get("/thoughts/{note_id}", response_class=HTMLResponse)
def thought_detail(note_id: int, request: Request, db: Session = Depends(get_session)):
    note = db.get(Note, note_id)
    if note is None:
        return HTMLResponse("Thought not found", status_code=404)
    if note.status == "draft" and not is_admin(request):
        item = {
            "id": note.id,
            "title": note.title or "",
            "kind": note.kind or "essay",
            "created_at": note.created_at,
            "target": resolve_target(db, note.target_type, note.target_id),
            "related_songs": related_songs_for_note(db, note.id),
            "teaser": _teaser(note.body),
        }
        return templates.TemplateResponse(request, "note_draft.html", {"it": item})
    if note.visibility == "subscribers" and not is_subscriber(request, db):
        item = {
            "id": note.id,
            "title": note.title or "",
            "kind": note.kind or "essay",
            "created_at": note.created_at,
            "target": resolve_target(db, note.target_type, note.target_id),
            "related_songs": related_songs_for_note(db, note.id),
            "teaser": _teaser(note.body),
        }
        return templates.TemplateResponse(request, "note_locked.html", {"it": item})
    comments = (
        db.query(Comment)
        .filter(Comment.note_id == note.id, Comment.approved == True)  # noqa: E712
        .order_by(Comment.created_at.asc())
        .all()
    )
    item = {
        "id": note.id,
        "title": note.title or "",
        "body_html": render_markdown(note.body),
        "kind": note.kind or "essay",
        "status": note.status or "published",
        "created_at": note.created_at,
        "updated_at": note.updated_at,
        "target": resolve_target(db, note.target_type, note.target_id),
        "related_songs": related_songs_for_note(db, note.id),
        "comment_count": len(comments),
        "comments": [
            {"id": c.id, "author_name": c.author_name, "body": c.body, "created_at": c.created_at}
            for c in comments
        ],
        "pending_comments": [
            {"id": c.id, "author_name": c.author_name, "body": c.body, "created_at": c.created_at}
            for c in (
                db.query(Comment)
                .filter(Comment.note_id == note.id, Comment.approved == False)  # noqa: E712
                .order_by(Comment.created_at.asc())
                .all()
            )
        ] if is_admin(request) else [],
    }
    return templates.TemplateResponse(request, "note_detail.html", {"it": item})


@app.get("/notes/new", response_class=HTMLResponse)
def notes_new(
    request: Request,
    target_type: str = "general",
    target_id: int | None = None,
    kind: str = "essay",
    status: str = "published",
    visibility: str = "public",
    db: Session = Depends(get_session),
):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    target = resolve_target(db, target_type, target_id) if target_type != "general" else None
    if kind not in ("essay", "review", "fragment", "note", "update"):
        kind = "essay"
    if status not in ("draft", "published"):
        status = "published"
    if visibility not in ("public", "subscribers"):
        visibility = "public"
    return templates.TemplateResponse(
        request, "notes_edit.html",
        {
            "note": None,
            "target_type": target_type,
            "target_id": target_id,
            "target": target,
            "related_songs": [],
            "initial_kind": kind,
            "initial_status": status,
            "initial_visibility": visibility,
        },
    )


@app.get("/notes/{note_id}/edit", response_class=HTMLResponse)
def notes_edit(note_id: int, request: Request, db: Session = Depends(get_session)):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    n = db.get(Note, note_id)
    if n is None:
        return HTMLResponse("Not found", status_code=404)
    target = resolve_target(db, n.target_type, n.target_id)
    return templates.TemplateResponse(
        request, "notes_edit.html",
        {
            "note": n,
            "target_type": n.target_type,
            "target_id": n.target_id,
            "target": target,
            "related_songs": related_songs_for_note(db, n.id),
            "initial_kind": n.kind,
            "initial_status": n.status,
            "initial_visibility": n.visibility,
        },
    )


class NoteBody(BaseModel):
    target_type: str = "general"
    target_id: int | None = None
    related_song_ids: list[int] = []
    title: str | None = None
    body: str = ""
    visibility: str = "public"
    status: str = "published"
    kind: str = "essay"


@app.post("/api/notes")
def create_note(body: NoteBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    if body.target_type not in ("song", "album", "artist", "general"):
        raise HTTPException(400, "invalid target_type")
    vis = body.visibility if body.visibility in ("public", "subscribers") else "public"
    status = body.status if body.status in ("draft", "published") else "published"
    kind = body.kind if body.kind in ("essay", "review", "fragment", "note", "update") else "essay"
    n = Note(
        target_type=body.target_type,
        target_id=body.target_id if body.target_type != "general" else None,
        title=body.title,
        body=body.body,
        visibility=vis,
        status=status,
        kind=kind,
    )
    db.add(n)
    db.commit()
    if body.related_song_ids:
        seen = set()
        for song_id in body.related_song_ids:
            if song_id in seen:
                continue
            seen.add(song_id)
            if db.get(Song, song_id) is not None:
                db.add(NoteSong(note_id=n.id, song_id=song_id))
        db.commit()
    invalidate_artist_scores_cache()
    return {"id": n.id}


@app.put("/api/notes/{note_id}")
def update_note(note_id: int, body: NoteBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    n = db.get(Note, note_id)
    if n is None:
        raise HTTPException(404, "note not found")
    n.title = body.title
    n.body = body.body
    if body.visibility in ("public", "subscribers"):
        n.visibility = body.visibility
    if body.status in ("draft", "published"):
        n.status = body.status
    if body.kind in ("essay", "review", "fragment", "note", "update"):
        n.kind = body.kind
    db.query(NoteSong).filter(NoteSong.note_id == n.id).delete()
    seen = set()
    for song_id in body.related_song_ids:
        if song_id in seen:
            continue
        seen.add(song_id)
        if db.get(Song, song_id) is not None:
            db.add(NoteSong(note_id=n.id, song_id=song_id))
    db.commit()
    invalidate_artist_scores_cache()
    return {"ok": True}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    n = db.get(Note, note_id)
    if n is None:
        raise HTTPException(404, "note not found")
    db.delete(n)
    db.commit()
    invalidate_artist_scores_cache()
    return {"ok": True}


@app.get("/api/target-search")
def api_target_search(q: str, db: Session = Depends(get_session)):
    if not q or len(q) < 2:
        return {"results": []}
    return {"results": search_targets(db, q)}


@app.get("/api/note-search")
def api_note_search(q: str, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    if not q or len(q) < 2:
        return {"results": []}
    return {"results": search_notes(db, q)}


# ---------- Admin login / logout ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login_submit(request: Request):
    body = (await request.body()).decode("utf-8", errors="ignore")
    password = (parse_qs(body).get("password") or [""])[0]
    resp = RedirectResponse("/", status_code=302)
    if not do_login(resp, password):
        return RedirectResponse("/login?error=1", status_code=302)
    return resp


@app.post("/logout")
def logout_submit(request: Request):
    resp = RedirectResponse("/", status_code=302)
    do_logout(request, resp)
    return resp


# ---------- Subscriber unlock / Ko-fi paywall ----------

@app.get("/unlock", response_class=HTMLResponse)
def unlock_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request, "unlock.html",
        {"error": bool(error), "kofi_url": KOFI_URL},
    )


@app.post("/unlock")
async def unlock_submit(request: Request, db: Session = Depends(get_session)):
    body = (await request.body()).decode("utf-8", errors="ignore")
    code = (parse_qs(body).get("code") or [""])[0]
    resp = RedirectResponse("/notes", status_code=302)
    if not unlock_subscriber(resp, code, db):
        return RedirectResponse("/unlock?error=1", status_code=302)
    return resp


@app.get("/lock")
def lock_route():
    resp = RedirectResponse("/", status_code=302)
    lock_subscriber(resp)
    return resp


@app.post("/api/kofi-webhook")
async def kofi_webhook(request: Request, db: Session = Depends(get_session)):
    """Ko-fi posts form-urlencoded with a single 'data' field containing JSON.
    See https://help.ko-fi.com/hc/en-us/articles/360004162298
    """
    raw = (await request.body()).decode("utf-8", errors="ignore")
    parsed = parse_qs(raw)
    data_str = (parsed.get("data") or [""])[0]
    if not data_str:
        # also accept JSON-bodied requests for testing convenience
        try:
            payload = json.loads(raw)
        except Exception:
            print("[kofi] empty/invalid webhook body")
            raise HTTPException(400, "missing data")
    else:
        try:
            payload = json.loads(data_str)
        except Exception:
            print("[kofi] invalid JSON in data field")
            raise HTTPException(400, "invalid json")

    token = payload.get("verification_token", "")
    if not KOFI_VERIFICATION_TOKEN or token != KOFI_VERIFICATION_TOKEN:
        print(f"[kofi] BAD token attempt: got={token!r}")
        raise HTTPException(403, "bad token")

    typ = payload.get("type", "")
    is_sub_payment = bool(payload.get("is_subscription_payment"))
    is_first = bool(payload.get("is_first_subscription_payment"))
    txn = payload.get("kofi_transaction_id")
    email = payload.get("email")
    tier = payload.get("tier_name") or "supporter"

    if typ == "Subscription" and is_first:
        # de-dupe by txn id
        existing = (
            db.query(Subscriber)
            .filter(Subscriber.kofi_transaction_id == txn)
            .first()
            if txn else None
        )
        if existing:
            print(f"[kofi] duplicate first-sub txn={txn}, code={existing.access_code}")
            return {"ok": True, "access_code": existing.access_code, "duplicate": True}
        code = _generate_code(db)
        sub = Subscriber(
            email=email,
            access_code=code,
            tier=tier,
            status="active",
            kofi_transaction_id=txn,
            started_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=40),
        )
        db.add(sub)
        db.commit()
        print(f"[kofi] NEW subscriber email={email} tier={tier} code={code}")
        return {"ok": True, "access_code": code}

    if typ == "Subscription" and is_sub_payment and not is_first:
        # renewal: bump expires_at and reactivate
        sub = None
        if email:
            sub = (
                db.query(Subscriber)
                .filter(Subscriber.email == email)
                .order_by(Subscriber.id.desc())
                .first()
            )
        if sub is None:
            print(f"[kofi] renewal for unknown email={email}, ignoring")
            return {"ok": True, "ignored": True}
        sub.status = "active"
        sub.expires_at = datetime.utcnow() + timedelta(days=40)
        db.commit()
        print(f"[kofi] renewal: subscriber {sub.id} extended")
        return {"ok": True, "renewed": True}

    if typ == "Donation":
        print(f"[kofi] donation from {payload.get('from_name')} ({email}) — no access granted")
        return {"ok": True, "donation": True}

    print(f"[kofi] unhandled type={typ}")
    return {"ok": True, "ignored": True}


# ---------- Subscriber admin dashboard ----------

@app.get("/subscribers", response_class=HTMLResponse)
def subscribers_page(request: Request, db: Session = Depends(get_session)):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    subs = db.query(Subscriber).order_by(Subscriber.id.desc()).all()
    return templates.TemplateResponse(request, "subscribers.html", {"subs": subs})


@app.post("/api/subscribers/{sub_id}/revoke")
def revoke_sub(sub_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    sub = db.get(Subscriber, sub_id)
    if sub is None:
        raise HTTPException(404, "not found")
    sub.status = "revoked"
    db.commit()
    return {"ok": True}


@app.post("/api/subscribers/{sub_id}/reactivate")
def reactivate_sub(sub_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    sub = db.get(Subscriber, sub_id)
    if sub is None:
        raise HTTPException(404, "not found")
    sub.status = "active"
    sub.expires_at = datetime.utcnow() + timedelta(days=40)
    db.commit()
    return {"ok": True}


class ManualSubBody(BaseModel):
    email: str | None = None
    tier: str | None = "supporter"
    days: int = 365


@app.post("/api/subscribers/manual")
def create_manual_sub(body: ManualSubBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    code = _generate_code(db)
    sub = Subscriber(
        email=body.email,
        access_code=code,
        tier=body.tier or "supporter",
        status="active",
        started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=max(1, int(body.days or 365))),
        notes="manual",
    )
    db.add(sub)
    db.commit()
    return {"ok": True, "id": sub.id, "access_code": code}


@app.post("/api/kofi-revoke")
def kofi_revoke(request: Request, db: Session = Depends(get_session), subscriber_id: int = Query(...)):
    require_admin(request)
    sub = db.get(Subscriber, subscriber_id)
    if sub is None:
        raise HTTPException(404, "not found")
    sub.status = "revoked"
    db.commit()
    return {"ok": True}


# ---------- Public comments on notes ----------

class CommentBody(BaseModel):
    author_name: str = "Anonymous"
    body: str


@app.post("/api/notes/{note_id}/comments")
def create_comment(note_id: int, body: CommentBody, request: Request, db: Session = Depends(get_session)):
    note = db.get(Note, note_id)
    if note is None:
        raise HTTPException(404, "note not found")
    if note.visibility == "subscribers" and not is_subscriber(request, db):
        raise HTTPException(403, "subscribers only")
    body_text = (body.body or "").strip()
    if not body_text:
        raise HTTPException(400, "empty comment")
    if len(body_text) > 4000:
        raise HTTPException(400, "too long")
    author = (body.author_name or "Anonymous").strip()[:80] or "Anonymous"
    approved = is_admin(request)
    c = Comment(note_id=note_id, author_name=author, body=body_text, approved=approved)
    db.add(c)
    db.commit()
    return {"id": c.id, "approved": approved}


@app.post("/api/comments/{comment_id}/approve")
def approve_comment(comment_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    c = db.get(Comment, comment_id)
    if c is None:
        raise HTTPException(404, "comment not found")
    c.approved = True
    db.commit()
    return {"ok": True}


@app.get("/moderation", response_class=HTMLResponse)
def moderation_page(request: Request, db: Session = Depends(get_session)):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    pending = (
        db.query(Comment)
        .filter(Comment.approved == False)  # noqa: E712
        .order_by(Comment.created_at.asc())
        .all()
    )
    items = []
    for comment in pending:
        note = db.get(Note, comment.note_id)
        if note is None:
            continue
        items.append(
            {
                "id": comment.id,
                "author_name": comment.author_name,
                "body": comment.body,
                "created_at": comment.created_at,
                "note_id": note.id,
                "note_title": note.title or "Untitled",
                "target": resolve_target(db, note.target_type, note.target_id),
            }
        )
    return templates.TemplateResponse(request, "moderation.html", {"items": items})


@app.delete("/api/comments/{comment_id}")
def delete_comment(comment_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    c = db.get(Comment, comment_id)
    if c is None:
        raise HTTPException(404, "comment not found")
    db.delete(c)
    db.commit()
    return {"ok": True}


# ---------- Stats / Analytics ----------

@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, db: Session = Depends(get_session)):
    liked_song_ids = {sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()}

    def bucket(key_fn, songs):
        counts: dict = {}
        for s in songs:
            k = normalize_genre(key_fn(s)) or "Unknown"
            counts[k] = counts.get(k, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])

    liked_songs = (
        db.query(Song).join(Album).join(Artist)
        .options(joinedload(Song.album).joinedload(Album.artist))
        .filter(Song.id.in_(liked_song_ids)).all()
    ) if liked_song_ids else []

    by_genre = bucket(lambda s: s.album.genre, liked_songs)
    by_decade = bucket(
        lambda s: f"{(s.album.year // 10) * 10}s" if s.album and s.album.year else None,
        liked_songs,
    )
    by_gender = cached_gender_breakdown(db)
    max_genre_count = max((v for _, v in by_genre), default=1)
    max_decade_count = max((v for _, v in by_decade), default=1)
    max_gender_count = max((count for _, count, _ in by_gender), default=1)

    total_songs_in_lib = cached_listened_song_count(db)
    total_liked = cached_liked_song_count(db)
    total_comparisons = db.query(func.count(Comparison.id)).scalar() or 0
    progress = cached_progress_metrics(db)

    # Best monthly playlists by average rating of included songs
    playlist_rows = [
        {
            "id": playlist_id,
            "name": name,
            "year": year,
            "month": month,
            "avg": float(avg),
            "count": int(count or 0),
        }
        for playlist_id, name, year, month, avg, count in (
            db.query(
                Playlist.id,
                Playlist.name,
                Playlist.year,
                Playlist.month,
                func.avg(Song.glicko_rating),
                func.count(PlaylistSong.id),
            )
            .join(PlaylistSong, PlaylistSong.playlist_id == Playlist.id)
            .join(Song, Song.id == PlaylistSong.song_id)
            .group_by(Playlist.id, Playlist.name, Playlist.year, Playlist.month)
            .all()
        )
        if avg is not None
    ]
    playlist_rows.sort(key=lambda r: -r["avg"])
    max_playlist_avg = max((row["avg"] for row in playlist_rows), default=1.0)

    top_artists = [
        {"artist_id": artist_id, "name": name, "score": float(avg or 0.0)}
        for artist_id, name, avg, liked_count in (
            db.query(
                Artist.id,
                Artist.name,
                func.avg(Song.glicko_rating).label("avg_rating"),
                func.count(func.distinct(Song.id)).label("liked_count"),
            )
            .join(SongCredit, SongCredit.artist_id == Artist.id)
            .join(Song, Song.id == SongCredit.song_id)
            .join(PlaylistSong, PlaylistSong.song_id == Song.id)
            .filter(SongCredit.role.in_(("primary", "featured")))
            .group_by(Artist.id, Artist.name)
            .having(func.count(func.distinct(Song.id)) >= 3)
            .order_by(func.avg(Song.glicko_rating).desc(), func.count(func.distinct(Song.id)).desc())
            .limit(10)
            .all()
        )
    ]
    max_artist_score = max((row["score"] for row in top_artists), default=1.0)

    return templates.TemplateResponse(
        request, "stats.html",
        {
            "total_songs_in_lib": total_songs_in_lib,
            "total_liked": total_liked,
            "total_comparisons": total_comparisons,
            "progress": progress,
            "by_genre": by_genre,
            "by_decade": by_decade,
            "by_gender": by_gender,
            "max_genre_count": max_genre_count,
            "max_decade_count": max_decade_count,
            "max_gender_count": max_gender_count,
            "playlist_rows": playlist_rows,
            "max_playlist_avg": max_playlist_avg,
            "top_artists": top_artists,
            "max_artist_score": max_artist_score,
        },
    )


@app.get("/api/stats/artist-map")
def artist_map_data(db: Session = Depends(get_session)):
    """City/region-level favorite artist map data.

    Loaded separately from /stats. This intentionally uses a lightweight
    liked-song credit query instead of the full artist scoring path.
    """
    place_rows: dict[str, dict] = {}
    us_region_rows: dict[str, dict] = {}
    rows = (
        db.query(
            Artist.id,
            Artist.name,
            Artist.country,
            Artist.origin_city,
            Artist.origin_region,
            Artist.origin_lat,
            Artist.origin_lon,
            func.count(func.distinct(Song.id)).label("liked_count"),
            func.avg(Song.glicko_rating).label("avg_rating"),
        )
        .join(SongCredit, SongCredit.artist_id == Artist.id)
        .join(Song, Song.id == SongCredit.song_id)
        .join(PlaylistSong, PlaylistSong.song_id == Song.id)
        .filter(SongCredit.role.in_(("primary", "featured")))
        .group_by(Artist.id, Artist.name, Artist.country)
        .order_by(func.count(func.distinct(Song.id)).desc(), func.avg(Song.glicko_rating).desc())
        .limit(500)
        .all()
    )
    unknown_artists = 0
    city_level_count = 0
    for artist_id, name, country, origin_city, origin_region, origin_lat, origin_lon, liked_count, avg_rating in rows:
        code = (country or "").strip().upper()
        city_hit = _city_lookup(origin_city, origin_region)
        lat = origin_lat if origin_lat is not None else (city_hit or {}).get("lat")
        lon = origin_lon if origin_lon is not None else (city_hit or {}).get("lon")
        region = _clean_region(origin_region)
        place_name = ""
        if origin_city:
            city_level_count += 1
            if region:
                place_name = f"{origin_city}, {region}"
            elif code:
                place_name = f"{origin_city}, {code}"
            else:
                place_name = origin_city
        else:
            meta = COUNTRY_CENTROIDS.get(code)
            if meta is None:
                unknown_artists += 1
                continue
            lat = meta["lat"]
            lon = meta["lon"]
            place_name = meta["name"]
        if lat is None or lon is None:
            unknown_artists += 1
            continue
        key = f"{round(float(lat), 3)}:{round(float(lon), 3)}:{place_name.lower()}"
        item = place_rows.setdefault(
            key,
            {
                "code": code,
                "name": place_name,
                "city": origin_city or "",
                "region": region,
                "us_region": _us_region(code, origin_region, origin_city),
                "lat": float(lat),
                "lon": float(lon),
                "artist_count": 0,
                "avg_score": 0.0,
                "liked_songs": 0,
                "artists": [],
            },
        )
        item["artist_count"] += 1
        item["avg_score"] += float(avg_rating or 0)
        item["liked_songs"] += int(liked_count or 0)
        if len(item["artists"]) < 8:
            item["artists"].append(
                {
                    "id": artist_id,
                    "name": name,
                    "score": round(float(avg_rating or 0)),
                    "liked_count": int(liked_count or 0),
                    "myks": myk_score(float(avg_rating or 0)),
                }
            )

        us_region = item["us_region"]
        if us_region:
            reg = us_region_rows.setdefault(
                us_region,
                {"region": us_region, "artist_count": 0, "liked_songs": 0, "avg_score": 0.0},
            )
            reg["artist_count"] += 1
            reg["liked_songs"] += int(liked_count or 0)
            reg["avg_score"] += float(avg_rating or 0)

    places = []
    for item in place_rows.values():
        if item["artist_count"]:
            item["avg_score"] = round(item["avg_score"] / item["artist_count"])
        places.append(item)
    places.sort(key=lambda r: (r["liked_songs"], r["artist_count"], r["avg_score"]), reverse=True)
    regions = []
    for item in us_region_rows.values():
        if item["artist_count"]:
            item["avg_score"] = round(item["avg_score"] / item["artist_count"])
        regions.append(item)
    regions.sort(key=lambda r: (r["liked_songs"], r["artist_count"]), reverse=True)
    return {
        "places": places,
        "countries": places,  # backward-compatible for stale clients
        "us_regions": regions,
        "unknown_artists": unknown_artists,
        "city_level_artists": city_level_count,
    }


def _gender_category_for_song(db: Session, song: Song) -> str:
    credits = (
        db.query(SongCredit)
        .filter(SongCredit.song_id == song.id, SongCredit.role.in_(("primary", "featured")))
        .all()
    )
    all_genders: set[str] = set()
    for credit in credits:
        all_genders |= _expand_artist_genders(db, credit.artist_id)
    named = {gender for gender in all_genders if gender in ("male", "female", "nonbinary")}
    if len(named) >= 2:
        return "mixed"
    if len(named) == 1:
        return next(iter(named))
    return "unknown"


@app.get("/stats/gender/{category}", response_class=HTMLResponse)
def stats_gender_detail(category: str, request: Request, db: Session = Depends(get_session)):
    allowed = {"male", "female", "nonbinary", "mixed", "unknown"}
    if category not in allowed:
        return HTMLResponse("Gender bucket not found", status_code=404)
    songs = (
        db.query(Song)
        .options(joinedload(Song.album).joinedload(Album.artist))
        .filter(Song.comparison_count > 0)
        .order_by(Song.glicko_rating.desc())
        .all()
    )
    songs_with_stars = []
    for song in songs:
        if _gender_category_for_song(db, song) == category:
            songs_with_stars.append((song, myk_score(song.glicko_rating, song.glicko_rd)))
    reviewed_song_ids = {
        tid for (tid,) in db.query(Note.target_id).filter(Note.target_type == "song", Note.target_id.isnot(None)).distinct().all()
    }
    return templates.TemplateResponse(
        request,
        "songs.html",
        {
            "songs_with_stars": songs_with_stars[:200],
            "q": f"gender:{category}",
            "limit": 200,
            "tier": None,
            "reviewed_ids": reviewed_song_ids,
            "render_myks": render_myks,
        },
    )


@app.get("/listening-notes", response_class=HTMLResponse)
def listening_notes_page(request: Request):
    return templates.TemplateResponse(request, "listening_notes.html", {})


@app.get("/comparisons", response_class=HTMLResponse)
def comparisons_page(request: Request, db: Session = Depends(get_session)):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    rows = (
        db.query(Comparison)
        .order_by(Comparison.id.desc())
        .limit(500)
        .all()
    )
    song_ids = {c.song_a_id for c in rows} | {c.song_b_id for c in rows} | {c.winner_id for c in rows if c.winner_id}
    songs = {
        s.id: s for s in db.query(Song).options(joinedload(Song.album).joinedload(Album.artist)).filter(Song.id.in_(song_ids)).all()
    } if song_ids else {}
    items = []
    for comp in rows:
        a = songs.get(comp.song_a_id)
        b = songs.get(comp.song_b_id)
        winner = songs.get(comp.winner_id) if comp.winner_id else None
        items.append({
            "id": comp.id,
            "a": a,
            "b": b,
            "winner": winner,
            "difficulty": comp.difficulty or "",
            "nostalgia": bool(comp.nostalgia),
            "created_at": comp.created_at,
        })
    return templates.TemplateResponse(request, "comparisons.html", {"items": items})


@app.get("/safety", response_class=HTMLResponse)
def safety_page(request: Request):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backups = [
        {"name": p.name, "size": p.stat().st_size, "mtime": datetime.fromtimestamp(p.stat().st_mtime)}
        for p in sorted(BACKUP_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    ]
    journal_size = JOURNAL_PATH.stat().st_size if JOURNAL_PATH.exists() else 0
    return templates.TemplateResponse(
        request,
        "safety.html",
        {
            "db_exists": DB_PATH.exists(),
            "comparison_count": comparison_count_in_db(DB_PATH) or 0,
            "backups": backups,
            "journal_exists": JOURNAL_PATH.exists(),
            "journal_size": journal_size,
        },
    )


@app.post("/api/safety/snapshot")
def api_safety_snapshot(request: Request):
    require_admin(request)
    path = snapshot_db("manual")
    return {"ok": True, "path": path}


@app.get("/api/safety/export-history")
def api_export_history(request: Request):
    require_admin(request)
    if not JOURNAL_PATH.exists():
        return JSONResponse({"error": "history log not found"}, status_code=404)
    return FileResponse(str(JOURNAL_PATH), filename=JOURNAL_PATH.name, media_type="application/x-ndjson")


@app.post("/api/safety/export-comparisons")
def api_export_comparisons(request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    path = export_comparisons_from_db(db, "manual")
    return {"ok": True, "path": path}


@app.get("/api/safety/export-comparisons/latest")
def api_export_latest_comparisons(request: Request):
    require_admin(request)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    exports = sorted(BACKUP_DIR.glob("comparisons-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not exports:
        return JSONResponse({"error": "comparison export not found"}, status_code=404)
    latest = exports[0]
    return FileResponse(str(latest), filename=latest.name, media_type="application/json")


@app.post("/api/safety/repair-known-artists")
def api_repair_known_artists(request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    snapshot_path = snapshot_db("pre-known-artist-repair")
    export_path = export_comparisons_from_db(db, "pre-known-artist-repair")
    stats = repair_known_artist_data(db)
    return {
        "ok": True,
        "snapshot_path": snapshot_path,
        "export_path": export_path,
        "stats": stats,
    }


class RestoreBackupBody(BaseModel):
    filename: str


@app.post("/api/safety/restore")
def api_restore_backup(body: RestoreBackupBody, request: Request):
    require_admin(request)
    filename = Path(body.filename).name
    target = BACKUP_DIR / filename
    if not target.exists():
        raise HTTPException(404, "backup not found")
    current_comparisons = comparison_count_in_db(DB_PATH) or 0
    backup_comparisons = comparison_count_in_db(target)
    if backup_comparisons is not None and backup_comparisons < current_comparisons:
        raise HTTPException(
            409,
            f"refusing restore: backup has {backup_comparisons} comparisons but current DB has {current_comparisons}",
        )
    from .db import SessionLocal

    db = SessionLocal()
    try:
        export_comparisons_from_db(db, "pre-restore")
    finally:
        db.close()
    snapshot_db("pre-restore")
    engine.dispose()
    shutil.copy2(target, DB_PATH)
    return {"ok": True, "message": "restored backup over music.db; restart the server now"}


# ---------- Comparisons ----------

def _song_payload(s: Song) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "album_id": s.album.id if s.album else None,
        "album": s.album.title if s.album else None,
        "artist_id": s.album.artist.id if s.album and s.album.artist else None,
        "artist": s.album.artist.name if s.album and s.album.artist else None,
        "year": s.album.year if s.album else None,
        "genre": s.album.genre if s.album else None,
        "track_number": s.track_number,
        "rating": round(s.glicko_rating, 1),
        "rd": round(s.glicko_rd, 1),
        "comparison_count": s.comparison_count,
        "play_count": s.play_count or 0,
        "skip_count": s.skip_count or 0,
        "apple_library_id": s.apple_library_id,
    }


COMPARISON_QUEUE_DEFAULT_PAIRS = 4
COMPARISON_QUEUE_MAX_PAIRS = 40


def _comparison_pair_key(a_id: int, b_id: int) -> tuple[int, int]:
    return tuple(sorted((int(a_id), int(b_id))))


def _active_comparison_queue(db: Session) -> list[ComparisonQueueItem]:
    return (
        db.query(ComparisonQueueItem)
        .filter(ComparisonQueueItem.status == "active")
        .order_by(ComparisonQueueItem.id.asc())
        .all()
    )


def _queue_track_payload(song: Song) -> dict:
    return {
        "song_id": song.id,
        "apple_library_id": song.apple_library_id,
        "type": "library-songs",
        "title": song.title,
        "artist": song.album.artist.name if song.album and song.album.artist else "",
        "album": song.album.title if song.album else "",
    }


def _queue_item_payload(item: ComparisonQueueItem, db: Session) -> dict | None:
    a = db.get(Song, item.song_a_id)
    b = db.get(Song, item.song_b_id)
    if a is None or b is None:
        item.status = "stale"
        item.completed_at = datetime.utcnow()
        db.flush()
        return None
    return {
        "queue_item_id": item.id,
        "a": _song_payload(a),
        "b": _song_payload(b),
    }


def _comparison_queue_payload(items: list[ComparisonQueueItem], db: Session) -> dict:
    pairs = []
    tracks_by_song_id: dict[int, dict] = {}
    for item in items:
        payload = _queue_item_payload(item, db)
        if payload is None:
            continue
        pairs.append(payload)
        for side in ("a", "b"):
            song_payload = payload[side]
            if song_payload.get("apple_library_id"):
                song = db.get(Song, song_payload["id"])
                if song is not None:
                    tracks_by_song_id[song.id] = _queue_track_payload(song)
    return {
        "pairs": pairs,
        "tracks": list(tracks_by_song_id.values()),
        "created_pairs": len(pairs),
    }


def _ensure_comparison_queue(db: Session, target_pairs: int = COMPARISON_QUEUE_DEFAULT_PAIRS) -> list[ComparisonQueueItem]:
    target_pairs = max(1, min(int(target_pairs or COMPARISON_QUEUE_DEFAULT_PAIRS), COMPARISON_QUEUE_MAX_PAIRS))
    active_items = _active_comparison_queue(db)
    active_pair_keys = {_comparison_pair_key(item.song_a_id, item.song_b_id) for item in active_items}
    active_song_ids = {sid for item in active_items for sid in (item.song_a_id, item.song_b_id)}
    attempts = 0
    max_attempts = max(120, target_pairs * 40)

    while len(active_items) < target_pairs and attempts < max_attempts:
        attempts += 1
        pair = pick_pair(db)
        if pair is None:
            break
        a, b = pair
        if not a.apple_library_id or not b.apple_library_id:
            continue
        pair_key = _comparison_pair_key(a.id, b.id)
        if pair_key in active_pair_keys:
            continue
        if a.id in active_song_ids or b.id in active_song_ids:
            continue

        item = ComparisonQueueItem(song_a_id=a.id, song_b_id=b.id, status="active")
        db.add(item)
        db.flush()
        note_recent_pair(a.id, b.id)
        active_items.append(item)
        active_pair_keys.add(pair_key)
        active_song_ids.add(a.id)
        active_song_ids.add(b.id)

    db.commit()
    return _active_comparison_queue(db)


def _complete_comparison_queue_item(db: Session, song_a_id: int, song_b_id: int) -> ComparisonQueueItem | None:
    pair_key = _comparison_pair_key(song_a_id, song_b_id)
    for item in _active_comparison_queue(db):
        if _comparison_pair_key(item.song_a_id, item.song_b_id) == pair_key:
            item.status = "completed"
            item.completed_at = datetime.utcnow()
            db.flush()
            return item
    return None


def _normalize_track_title(value: str) -> str:
    import re
    value = (value or "").lower().strip()
    value = re.sub(r"\s*\(.*?\)", "", value)
    value = re.sub(r"\s*\[.*?\]", "", value)
    value = value.replace("&", "and")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _album_track_rows(album: Album):
    liked_ids = {song.id for song in album.songs if song.liked}
    songs_by_norm: dict[str, list[Song]] = {}
    for song in album.songs:
        songs_by_norm.setdefault(_normalize_track_title(song.title), []).append(song)

    for bucket in songs_by_norm.values():
        bucket.sort(key=lambda s: (s.track_number is None, s.track_number or 9999, s.title.lower()))

    rows = []
    if album.tracks:
        for track in sorted(album.tracks, key=lambda t: t.position):
            matched_song = None
            bucket = songs_by_norm.get(_normalize_track_title(track.title)) or []
            if bucket:
                matched_song = bucket.pop(0)
            rows.append(
                {
                    "position": track.position,
                    "title": track.title,
                    "duration_ms": track.duration_ms,
                    "song": matched_song,
                    "liked": bool(matched_song and matched_song.id in liked_ids),
                    "known": matched_song is not None,
                }
            )
        leftovers = [song for bucket in songs_by_norm.values() for song in bucket]
        for song in sorted(leftovers, key=lambda s: (s.track_number is None, s.track_number or 9999, s.title.lower())):
            rows.append(
                {
                    "position": song.track_number,
                    "title": song.title,
                    "duration_ms": song.duration_ms,
                    "song": song,
                    "liked": song.id in liked_ids,
                    "known": True,
                }
            )
        return rows

    songs_sorted = sorted(album.songs, key=lambda s: (s.track_number is None, s.track_number or 9999, s.title.lower()))
    for idx, song in enumerate(songs_sorted, start=1):
        rows.append(
            {
                "position": song.track_number or idx,
                "title": song.title,
                "duration_ms": song.duration_ms,
                "song": song,
                "liked": song.id in liked_ids,
                "known": True,
            }
        )
    return rows


@app.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    config = get_apple_music_config()
    response = templates.TemplateResponse(request, "compare.html", {"apple_music_configured": config.configured})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/api/review-prompt")
def api_review_prompt(db: Session = Depends(get_session)):
    return {"prompt": any_review_candidate(db)}


@app.post("/api/undo-last")
def undo_last(request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    """Re-play the last N comparisons (except the most recent) to get a
    faithful undo of the last comparison's rating effect on both songs.
    Simpler: just delete the last comparison and re-run all comparisons for
    both affected songs from scratch. For a v1 we use the pragmatic approach:
    delete the last comparison record and recompute both songs' ratings from
    scratch by replaying their entire comparison history."""
    last = db.query(Comparison).order_by(Comparison.id.desc()).first()
    if last is None:
        raise HTTPException(404, "no comparisons yet")

    affected_ids = {last.song_a_id, last.song_b_id}
    db.delete(last)
    db.flush()

    # Reset affected songs and replay their histories.
    from .models import DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL

    # For each affected song we need the chronological list of comparisons it
    # was part of. We replay them in order, applying updates only to the
    # affected song (opponents keep their current ratings).
    for sid in affected_ids:
        song = db.get(Song, sid)
        if song is None:
            continue
        song.glicko_rating = DEFAULT_RATING
        song.glicko_rd = DEFAULT_RD
        song.glicko_vol = DEFAULT_VOL
        song.comparison_count = 0
        song.placement_pending = True
        song.placement_lo = None
        song.placement_hi = None

    db.flush()

    # Replay chronologically every remaining comparison that touches affected songs.
    for c in db.query(Comparison).order_by(Comparison.id.asc()).all():
        if c.song_a_id not in affected_ids and c.song_b_id not in affected_ids:
            continue
        a = db.get(Song, c.song_a_id)
        b = db.get(Song, c.song_b_id)
        if a is None or b is None:
            continue
        old_a = (a.glicko_rating, a.glicko_rd, a.glicko_vol)
        old_b = (b.glicko_rating, b.glicko_rd, b.glicko_vol)
        _apply_comparison_modifiers(a, b, c.winner_id, c.difficulty, bool(c.nostalgia))
        # Only update affected songs; opponents keep their current values.
        if a.id in affected_ids:
            a.comparison_count = (a.comparison_count or 0) + 1
            if a.placement_pending and c.winner_id is not None:
                update_bounds(a, b, c.winner_id == a.id)
                maybe_finalize(a)
        else:
            a.glicko_rating, a.glicko_rd, a.glicko_vol = old_a
        if b.id in affected_ids:
            b.comparison_count = (b.comparison_count or 0) + 1
            if b.placement_pending and c.winner_id is not None:
                update_bounds(b, a, c.winner_id == b.id)
                maybe_finalize(b)
        else:
            b.glicko_rating, b.glicko_rd, b.glicko_vol = old_b

    db.commit()
    append_event({"type": "undo", "comparison_id": last.id})
    return {"ok": True, "undone": last.id}


@app.get("/api/next-pair")
def next_pair(request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    active_items = _ensure_comparison_queue(db, 1)
    if not active_items:
        return JSONResponse({"error": "not enough songs"}, status_code=404)
    pair_payload = _queue_item_payload(active_items[0], db)
    if pair_payload is None:
        db.commit()
        return JSONResponse({"error": "not enough songs"}, status_code=404)
    total_comparisons = db.query(func.count(Comparison.id)).scalar() or 0
    return {
        **pair_payload,
        "total_comparisons": total_comparisons,
    }


@app.get("/api/next-pairs")
def next_pairs(request: Request, n: int = 4, db: Session = Depends(get_session)):
    require_admin(request)
    n = max(1, min(int(n), 8))
    active_items = _ensure_comparison_queue(db, n)
    payload = _comparison_queue_payload(active_items[:n], db)
    if len(payload["pairs"]) != len(active_items[:n]):
        db.commit()
    total_comparisons = db.query(func.count(Comparison.id)).scalar() or 0
    return {"pairs": payload["pairs"], "tracks": payload["tracks"], "total_comparisons": total_comparisons}


class CompareBody(BaseModel):
    song_a_id: int
    song_b_id: int
    winner_id: int | None  # null = skip/tie
    difficulty: str | None = None
    nostalgia: bool = False


@app.post("/api/compare")
def submit_comparison(body: CompareBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    a = db.get(Song, body.song_a_id)
    b = db.get(Song, body.song_b_id)
    if a is None or b is None:
        raise HTTPException(404, "song not found")
    if body.winner_id not in (a.id, b.id, None):
        raise HTTPException(400, "winner must be one of the two songs or null")
    if body.difficulty not in (None, "easy", "hard"):
        raise HTTPException(400, "difficulty must be easy, hard, or null")

    _apply_comparison_modifiers(a, b, body.winner_id, body.difficulty, body.nostalgia)
    a.comparison_count = (a.comparison_count or 0) + 1
    b.comparison_count = (b.comparison_count or 0) + 1

    # Update binary-search placement bounds for any pending songs.
    # A tie/skip does not update bounds.
    if body.winner_id is not None:
        a_won = body.winner_id == a.id
        if a.placement_pending:
            update_bounds(a, b, a_won)
            maybe_finalize(a)
        if b.placement_pending:
            update_bounds(b, a, not a_won)
            maybe_finalize(b)

    db.add(
        Comparison(
            song_a_id=a.id,
            song_b_id=b.id,
            winner_id=body.winner_id,
            difficulty=body.difficulty,
            nostalgia=body.nostalgia,
        )
    )
    db.commit()
    saved = db.query(Comparison).order_by(Comparison.id.desc()).first()
    if saved is not None:
        append_event(
            {
                "type": "compare",
                "comparison_id": saved.id,
                "song_a_id": saved.song_a_id,
                "song_b_id": saved.song_b_id,
                "winner_id": saved.winner_id,
                "difficulty": saved.difficulty,
                "nostalgia": bool(saved.nostalgia),
                "created_at": saved.created_at.isoformat() if saved.created_at else None,
            }
        )

    # Anti-repeat tracking
    note_recent_pair(a.id, b.id)
    completed_queue_item = _complete_comparison_queue_item(db, a.id, b.id)
    active_items = _ensure_comparison_queue(db, COMPARISON_QUEUE_DEFAULT_PAIRS)
    queue_payload = _comparison_queue_payload(active_items, db)
    invalidate_artist_scores_cache()

    return {
        "a": _song_payload(a),
        "b": _song_payload(b),
        "completed_queue_item_id": completed_queue_item.id if completed_queue_item else None,
        "queue": queue_payload,
        "next_pair": queue_payload["pairs"][0] if queue_payload["pairs"] else None,
    }


# ---------- People / Acts management ----------

@app.get("/api/person-search")
def api_person_search(q: str, db: Session = Depends(get_session)):
    if not q or len(q) < 1:
        return {"results": []}
    like = f"%{q}%"
    rows = db.query(Person).filter(Person.name.ilike(like)).order_by(Person.name).limit(20).all()
    return {"results": [{"id": p.id, "name": p.name, "gender": p.gender} for p in rows]}


@app.get("/api/artist-search")
def api_artist_search(q: str, db: Session = Depends(get_session)):
    if not q or len(q) < 1:
        return {"results": []}
    like = f"%{q}%"
    rows = db.query(Artist).filter(Artist.name.ilike(like)).order_by(Artist.name).limit(20).all()
    return {"results": [{"id": a.id, "name": a.name, "kind": a.kind} for a in rows]}


@app.get("/api/comparison-count")
def api_comparison_count(db: Session = Depends(get_session)):
    return {"count": db.query(func.count(Comparison.id)).scalar() or 0}


@app.get("/api/apple-music/weekly-comparison-candidates")
def api_weekly_comparison_candidates(request: Request, pairs: int = 12, db: Session = Depends(get_session)):
    require_admin(request)
    pairs = max(1, min(int(pairs or 12), 40))
    active_items = _ensure_comparison_queue(db, pairs)
    payload = _comparison_queue_payload(active_items[:pairs], db)
    return {
        "pairs": payload["pairs"],
        "tracks": payload["tracks"],
        "requested_pairs": pairs,
        "created_pairs": payload["created_pairs"],
        "playlist_name": "MYKMAN Comparisons",
        "queue_is_canonical": True,
    }


class CreateArtistBody(BaseModel):
    name: str
    kind: str = "solo"


@app.post("/api/artists")
def api_create_artist(body: CreateArtistBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name required")

    # Try MusicBrainz lookup first so we can dedupe against an mb_id
    mb_id = None
    canonical_name = name
    mb_kind = None
    try:
        from .musicbrainz import search_artist
        hits = search_artist(name) or []
        if hits:
            top = hits[0]
            # Only trust strong matches to avoid wrong auto-link
            if int(top.get("score", 0)) >= 90:
                mb_id = top.get("id")
                canonical_name = top.get("name") or name
                mb_type = (top.get("type") or "").lower()
                if mb_type == "group":
                    mb_kind = "group"
                elif mb_type == "person":
                    mb_kind = "solo"
    except Exception:
        pass

    # Dedupe by mb_id first, then by case-insensitive name
    if mb_id:
        existing = db.query(Artist).filter(Artist.mb_id == mb_id).one_or_none()
        if existing:
            return {"id": existing.id, "name": existing.name, "created": False, "mb_id": mb_id}
    existing = db.query(Artist).filter(func.lower(Artist.name) == canonical_name.lower()).one_or_none()
    if existing:
        if mb_id and not existing.mb_id:
            existing.mb_id = mb_id
            db.commit()
        return {"id": existing.id, "name": existing.name, "created": False, "mb_id": existing.mb_id}

    kind = body.kind if body.kind in ("solo", "group", "collab") else (mb_kind or "solo")
    a = Artist(name=canonical_name, kind=kind, mb_id=mb_id)
    db.add(a)
    db.commit()
    return {"id": a.id, "name": a.name, "created": True, "mb_id": mb_id}


class KindBody(BaseModel):
    kind: str  # solo|group|collab


@app.post("/api/artists/{artist_id}/kind")
def set_artist_kind(artist_id: int, body: KindBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    if body.kind not in ("solo", "group", "collab"):
        raise HTTPException(400, "invalid kind")
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    artist.kind = body.kind
    artist.prompt_resolved = True
    db.commit()
    return {"ok": True}


class MemberBody(BaseModel):
    person_id: int | None = None
    person_name: str | None = None
    person_gender: str | None = None
    child_artist_id: int | None = None
    role: str = "member"


class MergeArtistBody(BaseModel):
    source_artist_id: int


@app.post("/api/artists/{artist_id}/members")
def add_member(artist_id: int, body: MemberBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    if body.role not in ("member", "frontperson", "producer", "guest"):
        raise HTTPException(400, "invalid role")

    person_id = body.person_id
    child_id = body.child_artist_id

    if not person_id and not child_id and body.person_name:
        # create new person
        name = body.person_name.strip()
        if not name:
            raise HTTPException(400, "empty person name")
        existing = db.query(Person).filter(Person.name == name).first()
        if existing:
            person_id = existing.id
        else:
            gender = body.person_gender or "unknown"
            if gender not in ("male", "female", "nonbinary", "unknown"):
                gender = "unknown"
            p = Person(name=name, gender=gender)
            db.add(p)
            db.flush()
            person_id = p.id

    if not person_id and not child_id:
        raise HTTPException(400, "must provide person or child artist")
    if child_id and child_id == artist_id:
        raise HTTPException(400, "cannot add artist to itself")

    m = ArtistMembership(
        artist_id=artist_id,
        person_id=person_id,
        child_artist_id=child_id,
        role=body.role,
    )
    db.add(m)
    artist.prompt_resolved = True
    db.commit()
    return {"ok": True, "id": m.id}


@app.delete("/api/artists/{artist_id}/members/{membership_id}")
def remove_member(artist_id: int, membership_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    m = db.get(ArtistMembership, membership_id)
    if m is None or m.artist_id != artist_id:
        raise HTTPException(404, "membership not found")
    db.delete(m)
    db.commit()
    return {"ok": True}


@app.post("/api/artists/{artist_id}/merge")
def merge_artist_into(artist_id: int, body: MergeArtistBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    target = db.get(Artist, artist_id)
    source = db.get(Artist, body.source_artist_id)
    if target is None or source is None:
        raise HTTPException(404, "artist not found")
    if target.id == source.id:
        raise HTTPException(400, "cannot merge artist into itself")

    for album in db.query(Album).filter(Album.artist_id == source.id).all():
        exists = (
            db.query(Album)
            .filter(Album.artist_id == target.id, func.lower(Album.title) == album.title.lower())
            .first()
        )
        if exists is None:
            album.artist_id = target.id

    for credit in db.query(SongCredit).filter(SongCredit.artist_id == source.id).all():
        dup = (
            db.query(SongCredit)
            .filter(
                SongCredit.song_id == credit.song_id,
                SongCredit.artist_id == target.id,
                SongCredit.role == credit.role,
            )
            .first()
        )
        if dup is None:
            credit.artist_id = target.id
        else:
            db.delete(credit)

    for membership in db.query(ArtistMembership).filter(ArtistMembership.artist_id == source.id).all():
        dup = (
            db.query(ArtistMembership)
            .filter(
                ArtistMembership.artist_id == target.id,
                ArtistMembership.person_id == membership.person_id,
                ArtistMembership.child_artist_id == membership.child_artist_id,
                ArtistMembership.role == membership.role,
            )
            .first()
        )
        if dup is None:
            membership.artist_id = target.id
        else:
            db.delete(membership)

    for membership in db.query(ArtistMembership).filter(ArtistMembership.child_artist_id == source.id).all():
        dup = (
            db.query(ArtistMembership)
            .filter(
                ArtistMembership.artist_id == membership.artist_id,
                ArtistMembership.child_artist_id == target.id,
                ArtistMembership.role == membership.role,
            )
            .first()
        )
        if dup is None:
            membership.child_artist_id = target.id
        else:
            db.delete(membership)

    for rel in db.query(ArtistRelease).filter(ArtistRelease.artist_id == source.id).all():
        dup = (
            db.query(ArtistRelease)
            .filter(ArtistRelease.artist_id == target.id, ArtistRelease.release_group_mb_id == rel.release_group_mb_id)
            .first()
        )
        if dup is None:
            rel.artist_id = target.id
        else:
            db.delete(rel)

    if not target.mb_id and source.mb_id:
        target.mb_id = source.mb_id
    if not target.image_url and source.image_url:
        target.image_url = source.image_url
    if not target.image_path and source.image_path:
        target.image_path = source.image_path
    if not target.country and source.country:
        target.country = source.country
    if not target.disambiguation and source.disambiguation:
        target.disambiguation = source.disambiguation
    if not target.start_year and source.start_year:
        target.start_year = source.start_year
    if not target.end_year and source.end_year:
        target.end_year = source.end_year
    if not target.gender and source.gender:
        target.gender = source.gender
    if target.kind in (None, "solo") and source.kind in ("group", "collab"):
        target.kind = source.kind
    target.prompt_resolved = bool(target.prompt_resolved or source.prompt_resolved)
    if (target.internet_release_total or 0) < (source.internet_release_total or 0):
        target.internet_release_total = source.internet_release_total
    if (target.internet_track_total or 0) < (source.internet_track_total or 0):
        target.internet_track_total = source.internet_track_total

    note_targets = db.query(Note).filter(Note.target_type == "artist", Note.target_id == source.id).all()
    for note in note_targets:
        note.target_id = target.id

    db.flush()
    db.delete(source)
    db.commit()
    return {"ok": True, "target_id": target.id}


class QuickClassifyBody(BaseModel):
    kind: str
    gender: str | None = None  # for solo
    child_artist_ids: list[int] | None = None  # for collab


@app.post("/api/artists/{artist_id}/quick-classify")
def quick_classify(artist_id: int, body: QuickClassifyBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    if body.kind not in ("solo", "group", "collab"):
        raise HTTPException(400, "invalid kind")
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    artist.kind = body.kind

    if body.kind == "solo":
        gender = body.gender or "unknown"
        if gender not in ("male", "female", "nonbinary", "unknown"):
            gender = "unknown"
        person = db.query(Person).filter(Person.name == artist.name).first()
        if person is None:
            person = Person(name=artist.name, gender=gender)
            db.add(person)
            db.flush()
        else:
            if person.gender == "unknown" or gender != "unknown":
                person.gender = gender
        # Normalize any duplicate same-name person records that older backfills created.
        for other in db.query(Person).filter(Person.name == artist.name, Person.id != person.id).all():
            if other.gender == "unknown" or gender != "unknown":
                other.gender = gender
        # add membership if missing
        existing = (
            db.query(ArtistMembership)
            .filter(ArtistMembership.artist_id == artist.id, ArtistMembership.person_id == person.id)
            .first()
        )
        if existing is None:
            existing = ArtistMembership(artist_id=artist.id, person_id=person.id, role="member")
            db.add(existing)
            db.flush()
        # A solo act should resolve to one person membership, not a pile of aliases.
        extra_memberships = (
            db.query(ArtistMembership)
            .filter(ArtistMembership.artist_id == artist.id, ArtistMembership.id != existing.id)
            .all()
        )
        for membership in extra_memberships:
            if membership.person_id == person.id:
                continue
            db.delete(membership)
        # legacy mirror
        artist.gender = {"male": "M", "female": "F", "nonbinary": "NB", "unknown": "Unknown"}.get(gender, "Unknown")
        artist.is_band = False
    elif body.kind == "group":
        artist.gender = "Band"
        artist.is_band = True
    elif body.kind == "collab":
        artist.gender = "Band"
        artist.is_band = True
        for cid in (body.child_artist_ids or []):
            if cid == artist_id:
                continue
            existing = (
                db.query(ArtistMembership)
                .filter(
                    ArtistMembership.artist_id == artist.id,
                    ArtistMembership.child_artist_id == cid,
                )
                .first()
            )
            if existing is None:
                db.add(ArtistMembership(artist_id=artist.id, child_artist_id=cid, role="member"))

    artist.prompt_resolved = True
    db.commit()
    return {"ok": True}


# ---------- MusicBrainz enrichment ----------

from .enrich import enrich_artist as _enrich_artist, enrich_album as _enrich_album, bulk_enrich as _bulk_enrich, progress as _enrich_progress
from .db import SessionLocal as _SessionLocal


@app.post("/api/artists/{artist_id}/enrich")
def api_enrich_artist(artist_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    ar = db.get(Artist, artist_id)
    if ar is None:
        raise HTTPException(404, "artist not found")
    if is_various_artists_name(ar.name):
        return {"ok": False, "reason": "skip_various_artists"}
    return _enrich_artist(db, ar)


@app.post("/api/artists/{artist_id}/apple-catalog-enrich")
def api_apple_catalog_enrich_artist(artist_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    try:
        return _apple_catalog_enrich_artist(db, artist)
    except Exception as exc:
        db.rollback()
        return JSONResponse(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            status_code=500,
        )


@app.post("/api/artists/{artist_id}/apple-catalog-enrich-manual")
def api_apple_catalog_enrich_artist_manual(artist_id: int, body: AppleCatalogManualBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    catalog_artist_id = (body.catalog_artist_id or "").strip()
    if not catalog_artist_id:
        raise HTTPException(400, "catalog_artist_id is required")
    try:
        return _apple_catalog_enrich_artist(db, artist, catalog_artist_id=catalog_artist_id)
    except Exception as exc:
        db.rollback()
        return JSONResponse(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            status_code=500,
        )


@app.post("/api/artists/{artist_id}/apple-catalog-status")
def api_apple_catalog_status_artist(artist_id: int, body: AppleCatalogStatusBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    artist = db.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    status = (body.status or "").strip()
    if status == "no_match":
        artist.apple_catalog_status = "no_match"
        artist.internet_synced_at = datetime.utcnow()
    elif status == "clear":
        artist.apple_catalog_status = None
        artist.apple_catalog_id = None
        artist.internet_synced_at = None
    else:
        raise HTTPException(400, "status must be no_match or clear")
    db.commit()
    invalidate_artist_scores_cache()
    return {
        "ok": True,
        "artist_id": artist.id,
        "apple_catalog_status": artist.apple_catalog_status,
        "internet_synced_at": artist.internet_synced_at,
    }


@app.post("/api/artists/apple-catalog-enrich-batch")
def api_apple_catalog_enrich_batch(body: AppleCatalogBatchBody, request: Request, limit: int = 10, db: Session = Depends(get_session)):
    require_admin(request)
    limit = max(1, min(25, int(limit or 10)))
    candidates = []
    seen_ids: set[int] = set()
    for artist_id in body.artist_ids:
        if artist_id in seen_ids:
            continue
        seen_ids.add(artist_id)
        artist = db.get(Artist, artist_id)
        if artist is None or is_various_artists_name(artist.name):
            continue
        if body.force or artist.internet_synced_at is None:
            candidates.append(artist)
        if len(candidates) >= limit:
            break

    results = []
    for artist in candidates:
        try:
            results.append(_apple_catalog_enrich_artist(db, artist))
        except Exception as exc:
            db.rollback()
            results.append(
                {
                    "ok": False,
                    "artist_id": artist.id,
                    "artist_name": artist.name,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            )

    return {
        "ok": True,
        "requested": limit,
        "processed": len(results),
        "updated": sum(1 for row in results if row.get("ok")),
        "failed": sum(1 for row in results if not row.get("ok")),
        "results": results,
    }


@app.post("/api/albums/{album_id}/enrich")
def api_enrich_album(album_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    al = (
        db.query(Album)
        .options(joinedload(Album.artist))
        .filter(Album.id == album_id)
        .first()
    )
    if al is None:
        raise HTTPException(404, "album not found")
    return _enrich_album(db, al)


@app.post("/api/enrich-all")
def api_enrich_all(request: Request, background_tasks: BackgroundTasks):
    require_admin(request)
    if _enrich_progress["running"]:
        return JSONResponse({"ok": False, "reason": "already_running"}, status_code=409)
    background_tasks.add_task(_bulk_enrich, _SessionLocal)
    return JSONResponse({"ok": True}, status_code=202)


@app.get("/api/enrich-status")
def api_enrich_status():
    return dict(_enrich_progress)
