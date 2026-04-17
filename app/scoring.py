"""
Derived scoring for albums and artists, and 5-MYK tier assignment.

Philosophy:
- Song ratings come directly from Glicko-2.
- Album score = weighted mean of its songs' ratings, where songs NOT in any
  playlist are treated as "listened to but didn't make the month" and use a low
  anchor rating.
- Artist score = weighted mean of album scores, weighted by song count.
- MYK tiers use fixed Glicko rating cutoffs. Songs with high RD are "unrated"
  until the system has enough data to be confident.
"""
import heapq
from dataclasses import dataclass
from markupsafe import Markup
from sqlalchemy.orm import Session

from .models import Artist, Album, Song, PlaylistSong, SongCredit, ArtistMembership, Person
from .canonical import canonical_key, linked_song_groups

UNLIKED_ANCHOR = 1200.0
TIER_RD_THRESHOLD = 120.0
ARTIST_FULL_CONFIDENCE_COVERAGE = 0.60
ARTIST_LOW_COVERAGE_PENALTY = 100.0
TIER_CUTOFFS = [
    (1850, 5),
    (1700, 4),
    (1500, 3),
    (1300, 2),
    (0, 1),
]

VARIOUS_ARTISTS_NAMES = {
    "various artists",
}


def myk_tier(rating: float, rd: float) -> int | None:
    if rd > TIER_RD_THRESHOLD:
        return None
    for cutoff, myks in TIER_CUTOFFS:
        if rating >= cutoff:
            return myks
    return 1


def myk_score(rating: float, rd: float | None = None) -> float | None:
    """Map a Glicko-ish score to 1-5 MYKs, rounded to half-MYKs.

    Songs still require confidence through RD. Album/artist scores can pass
    rd=None because they are already derived aggregates.
    """
    if rd is not None and rd > TIER_RD_THRESHOLD:
        return None
    if rating >= 1900:
        raw = 5.0
    elif rating >= 1700:
        raw = 4.0 + ((rating - 1700) / 200.0)
    elif rating >= 1500:
        raw = 3.0 + ((rating - 1500) / 200.0)
    elif rating >= 1300:
        raw = 2.0 + ((rating - 1300) / 200.0)
    else:
        raw = 1.0 + max(0.0, min(1.0, (rating - 1000) / 300.0))
    return max(1.0, min(5.0, round(raw * 2) / 2))


# Backward-compatible alias while the codebase finishes the rename.
star_tier = myk_tier


def render_myks(count: float | int | None) -> str:
    if not count:
        return "-"
    value = max(0.0, min(5.0, float(count)))
    whole = int(value)
    has_half = (value - whole) >= 0.5
    badges = "".join(
        '<img src="/static/img/myk.png" alt="MYK" class="myk-badge">'
        for _ in range(whole)
    )
    if has_half:
        badges += (
            '<span class="myk-half" aria-hidden="true">'
            '<img src="/static/img/myk.png" alt="" class="myk-badge">'
            '</span>'
        )
    label = f"{value:g} MYKs"
    return Markup(f'<span class="myk-strip" aria-label="{label}" title="{label}">{badges}</span>')


def is_various_artists_name(name: str | None) -> bool:
    return ((name or "").strip().lower() in VARIOUS_ARTISTS_NAMES)


@dataclass
class AlbumScore:
    album_id: int
    artist_id: int
    title: str
    artist_name: str
    score: float
    song_count: int
    displayed_total_tracks: int | None
    liked_count: int
    avg_rd: float
    release_type: str


def effective_album_total_tracks(album: Album) -> int | None:
    if album.total_track_count:
        return int(album.total_track_count)
    if getattr(album, "tracks", None):
        return len(album.tracks)
    if getattr(album, "songs", None):
        return len(album.songs)
    return None


def is_rankable_album(album: Album) -> bool:
    title = (album.title or "").lower()
    total_tracks = effective_album_total_tracks(album) or 0
    if "single" in title:
        return False
    if total_tracks and total_tracks <= 3:
        return False
    return True


def classify_release_type(album: Album) -> str:
    title = (album.title or "").lower()
    if (album.release_group_type or "").lower() == "ep" or "ep" in title:
        return "ep"
    total_tracks = effective_album_total_tracks(album) or 0
    if "single" in title or (total_tracks and total_tracks <= 3):
        return "single"
    return "album"


@dataclass
class ArtistScore:
    artist_id: int
    name: str
    score: float
    listened_albums: int
    total_albums: int | None
    total_songs: int | None
    liked_songs: int
    listened_tracks: int
    known_tracks: int | None
    discography_percent: int | None


def _song_effective_rating(song: Song, is_liked: bool) -> float:
    if is_liked:
        return song.glicko_rating
    return min(UNLIKED_ANCHOR, song.glicko_rating)


def _album_score_row(album: Album, liked_ids: set[int]) -> AlbumScore | None:
    if not is_rankable_album(album):
        return None
    songs = album.songs
    if not songs:
        return None
    total = 0.0
    weight_sum = 0.0
    liked_count = 0
    rd_sum = 0.0
    for song in songs:
        is_liked = song.id in liked_ids
        if is_liked:
            liked_count += 1
        eff = _song_effective_rating(song, is_liked)
        weight = max(0.1, 1.0 - (song.glicko_rd / 350.0))
        total += eff * weight
        weight_sum += weight
        rd_sum += song.glicko_rd
    score = total / weight_sum if weight_sum else 0.0
    return AlbumScore(
        album_id=album.id,
        artist_id=album.artist.id if album.artist else 0,
        title=album.title,
        artist_name=album.artist.name if album.artist else "",
        score=score,
        song_count=len(songs),
        displayed_total_tracks=effective_album_total_tracks(album),
        liked_count=liked_count,
        avg_rd=rd_sum / len(songs),
        release_type=classify_release_type(album),
    )


def album_scores(db: Session) -> list[AlbumScore]:
    liked_ids = {sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()}
    results: list[AlbumScore] = []
    for album in db.query(Album).all():
        row = _album_score_row(album, liked_ids)
        if row is not None:
            results.append(row)
    results.sort(key=lambda row: row.score, reverse=True)
    return results


def album_score_for(db: Session, album: Album) -> AlbumScore | None:
    liked_ids = {sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()}
    return _album_score_row(album, liked_ids)


def _expand_artist_genders(db: Session, artist_id: int, depth: int = 0, seen: set | None = None) -> set[str]:
    if seen is None:
        seen = set()
    if depth > 3 or artist_id in seen:
        return set()
    seen.add(artist_id)
    genders: set[str] = set()
    memberships = db.query(ArtistMembership).filter(ArtistMembership.artist_id == artist_id).all()
    for membership in memberships:
        if membership.person_id is not None:
            person = db.get(Person, membership.person_id)
            if person is not None:
                genders.add(person.gender or "unknown")
        elif membership.child_artist_id is not None:
            genders |= _expand_artist_genders(db, membership.child_artist_id, depth + 1, seen)
    return genders


def gender_breakdown(db: Session) -> list[tuple[str, int, float]]:
    rated_songs = db.query(Song).filter(Song.comparison_count > 0).all()
    artist_cache: dict[int, set[str]] = {}

    def gset_for(artist_id: int) -> set[str]:
        if artist_id not in artist_cache:
            artist_cache[artist_id] = _expand_artist_genders(db, artist_id)
        return artist_cache[artist_id]

    bucket: dict[str, list[float]] = {"male": [], "female": [], "nonbinary": [], "mixed": [], "unknown": []}
    for song in rated_songs:
        credits = (
            db.query(SongCredit)
            .filter(SongCredit.song_id == song.id, SongCredit.role.in_(("primary", "featured")))
            .all()
        )
        all_genders: set[str] = set()
        for credit in credits:
            all_genders |= gset_for(credit.artist_id)
        named = {gender for gender in all_genders if gender in ("male", "female", "nonbinary")}
        if len(named) >= 2:
            bucket["mixed"].append(song.glicko_rating)
        elif len(named) == 1:
            bucket[next(iter(named))].append(song.glicko_rating)
        else:
            bucket["unknown"].append(song.glicko_rating)
    out = []
    for category in ("male", "female", "nonbinary", "mixed", "unknown"):
        ratings = bucket[category]
        avg = sum(ratings) / len(ratings) if ratings else 0.0
        out.append((category, len(ratings), avg))
    return out


def _artist_score_row(db: Session, artist: Artist, liked_ids: set[int], groups: dict[int, int]) -> ArtistScore | None:
    if is_various_artists_name(artist.name):
        return None
    total_weight = 0.0
    score_acc = 0.0
    credited_song_ids = {
        sid
        for (sid,) in db.query(SongCredit.song_id)
        .filter(SongCredit.artist_id == artist.id, SongCredit.role.in_(("primary", "featured")))
        .distinct()
        .all()
    }
    liked_songs = 0
    listened_albums = 0
    listened_tracks = 0
    seen_canonical: set[tuple[str, str, int] | tuple[str, int]] = set()
    for album in artist.albums:
        if not album.songs:
            continue
        if classify_release_type(album) == "single":
            continue
        album_total = 0.0
        album_weight = 0.0
        album_song_count = 0
        album_liked = False
        for song in album.songs:
            gid = groups.get(song.id)
            key = ("linked", gid) if gid is not None else canonical_key(song)
            if key in seen_canonical:
                continue
            seen_canonical.add(key)
            album_song_count += 1
            is_liked = song.id in liked_ids
            if is_liked and song.id in credited_song_ids:
                liked_songs += 1
                album_liked = True
            eff = _song_effective_rating(song, is_liked)
            weight = max(0.1, 1.0 - (song.glicko_rd / 350.0))
            album_total += eff * weight
            album_weight += weight
        if album_weight == 0:
            continue
        album_total_tracks = effective_album_total_tracks(album) or album_song_count
        if album.confirmed_listened or album_liked:
            listened_albums += 1
            listened_tracks += album_total_tracks
        album_score = album_total / album_weight
        album_w = len(album.songs)
        score_acc += album_score * album_w
        total_weight += album_w

    score = (score_acc / total_weight) if total_weight else 0.0

    featured_query = (
        db.query(Song)
        .join(SongCredit, SongCredit.song_id == Song.id)
        .filter(
            SongCredit.artist_id == artist.id,
            SongCredit.role == "featured",
            Song.id.in_(liked_ids),
        )
    )
    own_album_ids = [al.id for al in artist.albums]
    if own_album_ids:
        featured_query = featured_query.filter(~Song.album_id.in_(own_album_ids))
    featured_bonus_songs = featured_query.all()
    if featured_bonus_songs:
        bonus_avg = sum(song.glicko_rating for song in featured_bonus_songs) / len(featured_bonus_songs)
        score = (score * 0.9) + (bonus_avg * 0.1) if total_weight else bonus_avg

    internet_total_albums = artist.internet_release_total
    internet_total_tracks = artist.internet_track_total
    if internet_total_tracks and internet_total_tracks > 0:
        coverage = max(0.0, min(1.0, listened_tracks / internet_total_tracks))
        confidence = max(0.0, min(1.0, coverage / ARTIST_FULL_CONFIDENCE_COVERAGE))
        # Low-coverage artist scores are evidence, not verdicts. Shrink the
        # rating toward uncertainty and apply a small uncertainty penalty so
        # three great songs from a huge discography do not dominate the canon.
        score = 1500.0 + ((score - 1500.0) * confidence)
        score -= (1.0 - confidence) * ARTIST_LOW_COVERAGE_PENALTY
    return ArtistScore(
        artist_id=artist.id,
        name=artist.name,
        score=score,
        listened_albums=listened_albums,
        total_albums=internet_total_albums,
        total_songs=internet_total_tracks,
        liked_songs=liked_songs,
        listened_tracks=listened_tracks,
        known_tracks=internet_total_tracks,
        discography_percent=int(round((listened_tracks / internet_total_tracks) * 100)) if internet_total_tracks else None,
    )


def artist_scores(db: Session) -> list[ArtistScore]:
    liked_ids = {sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()}
    groups = linked_song_groups(db)
    results: list[ArtistScore] = []
    for artist in db.query(Artist).all():
        row = _artist_score_row(db, artist, liked_ids, groups)
        if row is not None:
            results.append(row)
    results.sort(key=lambda row: row.score, reverse=True)
    return results


def artist_score_for(db: Session, artist: Artist) -> ArtistScore | None:
    liked_ids = {sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()}
    groups = linked_song_groups(db)
    return _artist_score_row(db, artist, liked_ids, groups)


def top_artist_scores(db: Session, limit: int = 10) -> list[ArtistScore]:
    liked_ids = {sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()}
    groups = linked_song_groups(db)
    heap: list[tuple[float, int, ArtistScore]] = []
    for artist in db.query(Artist).yield_per(100):
        row = _artist_score_row(db, artist, liked_ids, groups)
        if row is None:
            continue
        item = (row.score, row.artist_id, row)
        if len(heap) < limit:
            heapq.heappush(heap, item)
        elif item[:2] > heap[0][:2]:
            heapq.heapreplace(heap, item)
    return [item[2] for item in sorted(heap, key=lambda x: (x[0], x[1]), reverse=True)]
