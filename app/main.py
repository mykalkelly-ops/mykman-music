import json
import os
import re
import shutil
import secrets as _secrets
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
    Person, ArtistMembership, SongCredit, Subscriber, AlbumTrack, ListenQueueItem, NoteSong, ArtistRelease, init_db,
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

app = FastAPI(title="MYKMAN Music")

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
    review_albums = loved_albums_needing_review(db) if is_admin(request) else []
    # Artist-wide review prompts currently require a full-library scoring pass.
    # Keep them off the homepage so a normal page load stays lightweight on Render.
    review_artists = []
    progress = progress_metrics(db)
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
def albums_page(request: Request, unknown_first: int = 0, db: Session = Depends(get_session)):
    reviewed = {
        tid for (tid,) in db.query(Note.target_id).filter(Note.target_type == "album", Note.target_id.isnot(None)).distinct().all()
    }
    covers = {aid: cp for (aid, cp) in db.query(Album.id, Album.cover_path).filter(Album.cover_path.isnot(None)).all()}
    all_albums = album_scores(db)
    albums_main = [a for a in all_albums if a.release_type == "album"]
    albums_eps = [a for a in all_albums if a.release_type == "ep"]
    if request.state.is_admin and unknown_first:
        albums_main.sort(key=lambda a: (a.displayed_total_tracks is not None, -a.score, a.title.lower()))
        albums_eps.sort(key=lambda a: (a.displayed_total_tracks is not None, -a.score, a.title.lower()))
    return templates.TemplateResponse(
        request,
        "albums.html",
        {
            "albums": albums_main,
            "eps": albums_eps,
            "reviewed_ids": reviewed,
            "covers": covers,
            "unknown_first": bool(unknown_first),
            "myk_score": myk_score,
            "render_myks": render_myks,
        },
    )


@app.get("/artists", response_class=HTMLResponse)
def artists_page(request: Request, db: Session = Depends(get_session)):
    reviewed = {
        tid for (tid,) in db.query(Note.target_id).filter(Note.target_type == "artist", Note.target_id.isnot(None)).distinct().all()
    }
    images = {aid: ip for (aid, ip) in db.query(Artist.id, Artist.image_path).filter(Artist.image_path.isnot(None)).all()}
    return templates.TemplateResponse(
        request,
        "artists.html",
        {"artists": artist_scores(db), "reviewed_ids": reviewed, "images": images, "myk_score": myk_score, "render_myks": render_myks},
    )


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


class AlbumDecisionBody(BaseModel):
    listened: bool


class AlbumMetaBody(BaseModel):
    total_track_count: int | None = None


class ListenQueueBody(BaseModel):
    target_type: str
    target_id: int
    note: str | None = None


@app.post("/api/albums/{album_id}/listened")
def set_album_listened(album_id: int, body: AlbumDecisionBody, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(404, "album not found")
    album.confirmed_listened = bool(body.listened)
    album.excluded_from_listened = not bool(body.listened)
    db.commit()
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
        return {"artist": None}
    return {"artist": {"id": artist.id, "name": artist.name, "kind": artist.kind}}


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
    db: Session = Depends(get_session),
):
    if not is_admin(request):
        return RedirectResponse("/login", status_code=302)
    target = resolve_target(db, target_type, target_id) if target_type != "general" else None
    return templates.TemplateResponse(
        request, "notes_edit.html",
        {"note": None, "target_type": target_type, "target_id": target_id, "target": target, "related_songs": []},
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
        {"note": n, "target_type": n.target_type, "target_id": n.target_id, "target": target, "related_songs": related_songs_for_note(db, n.id)},
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
    return {"ok": True}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int, request: Request, db: Session = Depends(get_session)):
    require_admin(request)
    n = db.get(Note, note_id)
    if n is None:
        raise HTTPException(404, "note not found")
    db.delete(n)
    db.commit()
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
        .filter(Song.id.in_(liked_song_ids)).all()
    ) if liked_song_ids else []

    by_genre = bucket(lambda s: s.album.genre, liked_songs)
    by_decade = bucket(
        lambda s: f"{(s.album.year // 10) * 10}s" if s.album and s.album.year else None,
        liked_songs,
    )
    by_gender = gender_breakdown(db)
    max_genre_count = max((v for _, v in by_genre), default=1)
    max_decade_count = max((v for _, v in by_decade), default=1)
    max_gender_count = max((count for _, count, _ in by_gender), default=1)

    total_songs_in_lib = _listened_song_count(db)
    total_liked = _liked_song_count(db)
    total_comparisons = db.query(func.count(Comparison.id)).scalar() or 0
    progress = progress_metrics(db)

    # Best monthly playlists by average rating of included songs
    playlist_rows = []
    for p in db.query(Playlist).all():
        avg = (
            db.query(func.avg(Song.glicko_rating))
            .join(PlaylistSong, PlaylistSong.song_id == Song.id)
            .filter(PlaylistSong.playlist_id == p.id)
            .scalar()
        )
        if avg is not None:
            count = (
                db.query(func.count(PlaylistSong.id))
                .filter(PlaylistSong.playlist_id == p.id)
                .scalar()
                or 0
            )
            playlist_rows.append({"id": p.id, "name": p.name, "year": p.year, "month": p.month, "avg": float(avg), "count": count})
    playlist_rows.sort(key=lambda r: -r["avg"])
    max_playlist_avg = max((row["avg"] for row in playlist_rows), default=1.0)

    top_artists = top_artist_scores(db, limit=10)
    max_artist_score = max((row.score for row in top_artists), default=1.0)

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
    }


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
    return templates.TemplateResponse(request, "compare.html", {})


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
def next_pair(db: Session = Depends(get_session)):
    pair = pick_pair(db)
    if pair is None:
        return JSONResponse({"error": "not enough songs"}, status_code=404)
    a, b = pair
    from .pair_selector import note_recent_pair
    note_recent_pair(a.id, b.id)
    total_comparisons = db.query(func.count(Comparison.id)).scalar() or 0
    return {
        "a": _song_payload(a),
        "b": _song_payload(b),
        "total_comparisons": total_comparisons,
    }


@app.get("/api/next-pairs")
def next_pairs(n: int = 4, db: Session = Depends(get_session)):
    from .pair_selector import note_recent_pair
    n = max(1, min(int(n), 8))
    pairs = []
    seen_ids: set[int] = set()
    seen_pairs: set[tuple[int, int]] = set()
    attempts = 0
    max_attempts = max(12, n * 8)
    while len(pairs) < n and attempts < max_attempts:
        attempts += 1
        p = pick_pair(db)
        if p is None:
            break
        a, b = p
        pair_key = tuple(sorted((a.id, b.id)))
        # Avoid the same song or same exact pair appearing twice within one batch
        if a.id in seen_ids or b.id in seen_ids or pair_key in seen_pairs:
            continue
        seen_ids.add(a.id); seen_ids.add(b.id)
        seen_pairs.add(pair_key)
        note_recent_pair(a.id, b.id)
        pairs.append({"a": _song_payload(a), "b": _song_payload(b)})
    total_comparisons = db.query(func.count(Comparison.id)).scalar() or 0
    return {"pairs": pairs, "total_comparisons": total_comparisons}


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

    return {
        "a": _song_payload(a),
        "b": _song_payload(b),
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
