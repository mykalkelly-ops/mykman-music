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
from dataclasses import dataclass
from markupsafe import Markup
from sqlalchemy.orm import Session

from .models import Artist, Album, Song, PlaylistSong, SongCredit, ArtistMembership, Person
from .canonical import canonical_key, linked_song_groups

UNLIKED_ANCHOR = 1200.0
TIER_RD_THRESHOLD = 120.0
TIER_CUTOFFS = [
    (1850, 5),
    (1700, 4),
    (1500, 3),
    (1300, 2),
    (0, 1),
]


def myk_tier(rating: float, rd: float) -> int | None:
    if rd > TIER_RD_THRESHOLD:
        return None
    for cutoff, myks in TIER_CUTOFFS:
        if rating >= cutoff:
            return myks
    return 1


# Backward-compatible alias while the codebase finishes the rename.
star_tier = myk_tier


def render_myks(count: int | None) -> str:
    if not count:
        return "-"
    badges = "".join(
        '<img src="/static/img/myk.png" alt="MYK" class="myk-badge">'
        for _ in range(max(0, int(count)))
    )
    return Markup(f'<span class="myk-strip" aria-label="{count} MYKs">{badges}</span>')


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


def is_rankable_album(album: Album) -> bool:
    title = (album.title or "").lower()
    total_tracks = album.total_track_count or 0
    if "single" in title:
        return False
    if total_tracks and total_tracks <= 3:
        return False
    return True


def classify_release_type(album: Album) -> str:
    title = (album.title or "").lower()
    if (album.release_group_type or "").lower() == "ep" or "ep" in title:
        return "ep"
    if "single" in title or ((album.total_track_count or 0) and (album.total_track_count or 0) <= 3):
        return "single"
    return "album"


@dataclass
class ArtistScore:
    artist_id: int
    name: str
    score: float
    album_count: int
    total_songs: int
    liked_songs: int
    listened_tracks: int
    known_tracks: int
    discography_percent: int


def _song_effective_rating(song: Song, is_liked: bool) -> float:
    if is_liked:
        return song.glicko_rating
    return min(UNLIKED_ANCHOR, song.glicko_rating)


def album_scores(db: Session) -> list[AlbumScore]:
    liked_ids = {sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()}
    results: list[AlbumScore] = []
    albums = db.query(Album).all()
    for album in albums:
        if not is_rankable_album(album):
            continue
        songs = album.songs
        if not songs:
            continue
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
        results.append(
            AlbumScore(
                album_id=album.id,
                artist_id=album.artist.id if album.artist else 0,
                title=album.title,
                artist_name=album.artist.name if album.artist else "",
                score=score,
                song_count=len(songs),
                displayed_total_tracks=album.total_track_count or None,
                liked_count=liked_count,
                avg_rd=rd_sum / len(songs),
                release_type=classify_release_type(album),
            )
        )
    results.sort(key=lambda row: row.score, reverse=True)
    return results


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


def artist_scores(db: Session) -> list[ArtistScore]:
    liked_ids = {sid for (sid,) in db.query(PlaylistSong.song_id).distinct().all()}
    groups = linked_song_groups(db)
    results: list[ArtistScore] = []
    artists = db.query(Artist).all()
    for artist in artists:
        total_weight = 0.0
        score_acc = 0.0
        total_songs = 0
        liked_songs = 0
        album_count = 0
        listened_tracks = 0
        known_tracks = 0
        seen_canonical: set[tuple[str, str, int] | tuple[str, int]] = set()
        for album in artist.albums:
            if not album.songs:
                continue
            if classify_release_type(album) == "single":
                continue
            album_count += 1
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
                total_songs += 1
                album_song_count += 1
                is_liked = song.id in liked_ids
                if is_liked:
                    liked_songs += 1
                    album_liked = True
                eff = _song_effective_rating(song, is_liked)
                weight = max(0.1, 1.0 - (song.glicko_rd / 350.0))
                album_total += eff * weight
                album_weight += weight
            if album_weight == 0:
                continue
            album_total_tracks = album.total_track_count or album_song_count
            known_tracks += album_total_tracks
            if album.confirmed_listened or album_liked:
                listened_tracks += album_total_tracks
            album_score = album_total / album_weight
            album_w = len(album.songs)
            score_acc += album_score * album_w
            total_weight += album_w
        if total_weight == 0:
            continue
        score = score_acc / total_weight
        results.append(
            ArtistScore(
                artist_id=artist.id,
                name=artist.name,
                score=score,
                album_count=album_count,
                total_songs=total_songs,
                liked_songs=liked_songs,
                listened_tracks=listened_tracks,
                known_tracks=known_tracks,
                discography_percent=int(round((listened_tracks / known_tracks) * 100)) if known_tracks else 0,
            )
        )
    results.sort(key=lambda row: row.score, reverse=True)
    return results
