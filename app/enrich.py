"""Enrichment logic: MusicBrainz -> Artist/Album/Person DB rows + art caching."""
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from . import musicbrainz as mb
from . import art
from .models import Artist, Album, Person, ArtistMembership, AlbumTrack, ArtistRelease
from .scoring import effective_album_total_tracks, is_various_artists_name

# Module-level progress for the bulk background task.
progress = {
    "running": False,
    "done": 0,
    "total": 0,
    "remaining": 0,
    "current": "",
    "error": None,
}

BULK_ENRICH_BATCH_SIZE = 50
ENRICH_RETRY_COOLDOWN_HOURS = 12


def _parse_year(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(str(s)[:4])
    except Exception:
        return None


def _set_artist_origin_from_area(db: Session, artist: Artist, area: dict | None) -> None:
    if not area:
        return
    if not artist.origin_city and area.get("name"):
        artist.origin_city = area.get("name")
    if not artist.country and area.get("country"):
        artist.country = area.get("country")
    coords = area.get("coordinates") or {}
    try:
        if artist.origin_lat is None and coords.get("latitude") is not None:
            artist.origin_lat = float(coords.get("latitude"))
        if artist.origin_lon is None and coords.get("longitude") is not None:
            artist.origin_lon = float(coords.get("longitude"))
    except Exception:
        pass
    if not artist.origin_region:
        for rel in area.get("relations", []) or []:
            if rel.get("type") != "part of":
                continue
            parent = rel.get("area") or {}
            if parent.get("name"):
                artist.origin_region = parent.get("name")
                break


def enrich_artist(db: Session, artist: Artist) -> dict:
    """Look up artist by name (or existing mb_id), fill metadata + members + image."""
    if is_various_artists_name(artist.name):
        artist.internet_release_total = artist.internet_release_total or 0
        artist.internet_track_total = artist.internet_track_total or 0
        artist.internet_synced_at = datetime.utcnow()
        db.commit()
        return {"ok": False, "reason": "skip_various_artists"}

    mbid = artist.mb_id
    if not mbid:
        results = mb.search_artist(artist.name)
        if not results:
            artist.internet_synced_at = datetime.utcnow()
            db.commit()
            return {"ok": False, "reason": "no_match"}
        mbid = results[0]["id"]
        artist.mb_id = mbid
        top = results[0]
        if not artist.country and top.get("country"):
            artist.country = top["country"]
        if not artist.disambiguation and top.get("disambiguation"):
            artist.disambiguation = top["disambiguation"]
        ls = top.get("life-span") or {}
        artist.start_year = artist.start_year or _parse_year(ls.get("begin"))
        artist.end_year = artist.end_year or _parse_year(ls.get("end"))
        if (top.get("type") or "").lower() == "group" and artist.kind in (None, "solo"):
            artist.kind = "group"
        _set_artist_origin_from_area(db, artist, top.get("begin-area") or top.get("area"))

    detail = mb.get_artist(mbid) or {}
    if detail:
        if not artist.country and detail.get("country"):
            artist.country = detail["country"]
        if not artist.disambiguation and detail.get("disambiguation"):
            artist.disambiguation = detail["disambiguation"]
        ls = detail.get("life-span") or {}
        if not artist.start_year:
            artist.start_year = _parse_year(ls.get("begin"))
        if not artist.end_year:
            artist.end_year = _parse_year(ls.get("end"))
        if (detail.get("type") or "").lower() == "group" and artist.kind in (None, "solo"):
            artist.kind = "group"
        origin_area = detail.get("begin-area") or detail.get("area")
        _set_artist_origin_from_area(db, artist, origin_area)
        if origin_area and (artist.origin_lat is None or artist.origin_lon is None or not artist.origin_region):
            area_id = origin_area.get("id")
            if area_id:
                _set_artist_origin_from_area(db, artist, mb.get_area(area_id))

        # Members: artist-rels with type "member of band" where direction=backward
        # means the related entity is a member of this band.
        members_added = 0
        for rel in detail.get("relations", []) or []:
            if rel.get("type") != "member of band":
                continue
            rel_artist = rel.get("artist") or {}
            if not rel_artist or rel_artist.get("type") != "Person":
                continue
            pmbid = rel_artist.get("id")
            pname = rel_artist.get("name")
            if not pname:
                continue
            person = None
            if pmbid:
                person = db.query(Person).filter(Person.mb_id == pmbid).first()
            if person is None:
                person = db.query(Person).filter(Person.name == pname).first()
            if person is None:
                gender_raw = (rel_artist.get("gender") or "").lower()
                gender = gender_raw if gender_raw in ("male", "female", "nonbinary") else "unknown"
                person = Person(name=pname, gender=gender, mb_id=pmbid)
                db.add(person)
                db.flush()
            else:
                if not person.mb_id and pmbid:
                    person.mb_id = pmbid
            # membership if missing
            existing = (
                db.query(ArtistMembership)
                .filter(
                    ArtistMembership.artist_id == artist.id,
                    ArtistMembership.person_id == person.id,
                )
                .first()
            )
            if existing is None:
                attrs = rel.get("attributes") or []
                role = "member"
                if any(a.lower() == "original" for a in attrs):
                    role = "member"
                m = ArtistMembership(
                    artist_id=artist.id,
                    person_id=person.id,
                    role=role,
                    start_year=_parse_year((rel.get("begin") or "")),
                    end_year=_parse_year((rel.get("end") or "")),
                )
                db.add(m)
                members_added += 1

        if artist.kind == "group":
            # Remove the fake self-person created by older backfills.
            self_person = db.query(Person).filter(Person.name == artist.name).first()
            if self_person is not None:
                for membership in (
                    db.query(ArtistMembership)
                    .filter(ArtistMembership.artist_id == artist.id, ArtistMembership.person_id == self_person.id)
                    .all()
                ):
                    db.delete(membership)
            # Deduplicate duplicate memberships to the same person.
            seen_people: set[int] = set()
            memberships = (
                db.query(ArtistMembership)
                .filter(ArtistMembership.artist_id == artist.id, ArtistMembership.person_id.isnot(None))
                .order_by(ArtistMembership.id.asc())
                .all()
            )
            for membership in memberships:
                if membership.person_id in seen_people:
                    db.delete(membership)
                else:
                    seen_people.add(int(membership.person_id))

        # Wikidata image via url-rels
        if not artist.image_url:
            wd_url = None
            for rel in detail.get("relations", []) or []:
                if rel.get("type") == "wikidata":
                    wd_url = (rel.get("url") or {}).get("resource")
                    break
            if wd_url:
                # Reuse helper which re-fetches; but we already have detail,
                # so extract QID here and call wikidata directly via module.
                img = mb.get_wikidata_image_url(mbid)
                if img:
                    artist.image_url = img

    # Internet-backed discography totals (albums + EPs, excluding singles).
    release_groups = []
    for release_type in ("album", "ep"):
        release_groups.extend(mb.browse_release_groups(mbid, primary_type=release_type))
    seen_rg: set[str] = set()
    filtered_groups: list[dict] = []
    for rg in release_groups:
        rgid = rg.get("id")
        if not rgid or rgid in seen_rg:
            continue
        seen_rg.add(rgid)
        secondary_types = {(t or "").lower() for t in (rg.get("secondary-types") or [])}
        if "compilation" in secondary_types or "live" in secondary_types:
            continue
        filtered_groups.append(rg)

    release_total = len(filtered_groups)
    track_total = 0
    local_by_rg = {
        al.release_group_mb_id: al
        for al in db.query(Album).filter(Album.artist_id == artist.id, Album.release_group_mb_id.isnot(None)).all()
    }
    existing_release_rows = {
        row.release_group_mb_id: row
        for row in db.query(ArtistRelease).filter(ArtistRelease.artist_id == artist.id).all()
    }
    seen_release_groups: set[str] = set()
    for rg in filtered_groups:
        rgid = rg.get("id")
        if not rgid:
            continue
        seen_release_groups.add(rgid)
        local_album = local_by_rg.get(rgid)
        release_row = existing_release_rows.get(rgid)
        primary_type = (rg.get("primary-type") or "").lower() or None
        first_release = _parse_year(rg.get("first-release-date"))
        if release_row is None:
            release_row = ArtistRelease(
                artist_id=artist.id,
                release_group_mb_id=rgid,
                title=rg.get("title") or "Untitled",
                year=first_release,
                primary_type=primary_type,
            )
            db.add(release_row)
            existing_release_rows[rgid] = release_row
        else:
            release_row.title = rg.get("title") or release_row.title
            release_row.year = release_row.year or first_release
            release_row.primary_type = release_row.primary_type or primary_type
        if local_album and local_album.total_track_count:
            track_total += int(local_album.total_track_count)
            release_row.track_count = local_album.total_track_count
            continue
        releases = mb.browse_releases_for_release_group(rgid)
        chosen = None
        for rel in releases:
            if (rel.get("status") or "").lower() in ("official", ""):
                chosen = rel
                break
        if chosen is None and releases:
            chosen = releases[0]
        if chosen is None:
            continue
        detail = mb.get_release(chosen.get("id"))
        if not detail:
            continue
        total_tracks = 0
        for medium in detail.get("media", []) or []:
            total_tracks += int(medium.get("track-count") or 0)
        if total_tracks:
            track_total += total_tracks
            release_row.track_count = total_tracks

    for rgid, row in existing_release_rows.items():
        if rgid not in seen_release_groups:
            db.delete(row)

    artist.internet_release_total = int(release_total)
    artist.internet_track_total = int(track_total)
    artist.internet_synced_at = datetime.utcnow()

    db.commit()
    # Download image
    if artist.image_url and not artist.image_path:
        art.cache_artist_image(artist, db)
    return {
        "ok": True,
        "mb_id": artist.mb_id,
        "image": artist.image_path,
        "internet_release_total": artist.internet_release_total,
        "internet_track_total": artist.internet_track_total,
    }


def enrich_album(db: Session, album: Album) -> dict:
    if album.artist is None:
        return {"ok": False, "reason": "no_artist"}
    if is_various_artists_name(album.artist.name):
        return {"ok": False, "reason": "skip_various_artists_album"}
    if not album.release_group_mb_id:
        rg = mb.search_release_group(album.artist.name, album.title)
        if not rg:
            return {"ok": False, "reason": "no_match"}
        album.release_group_mb_id = rg.get("id")
        album.release_group_type = (rg.get("primary-type") or "").lower() or None
        if not album.year:
            album.year = _parse_year(rg.get("first-release-date"))
    elif not album.release_group_type:
        rg = mb.get_release_group(album.release_group_mb_id)
        if rg:
            album.release_group_type = (rg.get("primary-type") or "").lower() or None
    existing_track_count = len(album.tracks) if getattr(album, "tracks", None) is not None else 0
    found_detail = False
    if not album.mb_id or not album.total_track_count or existing_track_count == 0:
        detail = None
        if album.mb_id:
            detail = mb.get_release(album.mb_id)
        if detail is None:
            rel = mb.search_release(album.artist.name, album.title)
            if rel:
                album.mb_id = album.mb_id or rel.get("id")
                detail = mb.get_release(rel.get("id"))
        if detail:
            found_detail = True
            total_tracks = 0
            track_rows: list[dict] = []
            for medium in detail.get("media", []) or []:
                total_tracks += int(medium.get("track-count") or 0)
                for track in medium.get("tracks", []) or []:
                    try:
                        position = int(track.get("position") or 0)
                    except Exception:
                        position = 0
                    if position <= 0:
                        continue
                    recording = track.get("recording") or {}
                    track_rows.append(
                        {
                            "position": position,
                            "title": track.get("title") or recording.get("title") or "",
                            "duration_ms": track.get("length"),
                            "recording_mb_id": recording.get("id"),
                        }
                    )
            if total_tracks and not album.total_track_count:
                album.total_track_count = total_tracks
            if track_rows:
                db.query(AlbumTrack).filter(AlbumTrack.album_id == album.id).delete()
                for row in sorted(track_rows, key=lambda r: r["position"]):
                    db.add(AlbumTrack(album_id=album.id, **row))
    if album.release_group_mb_id and not album.cover_url:
        album.cover_url = mb.get_cover_art_url(album.release_group_mb_id)
    db.commit()
    if album.cover_url and not album.cover_path:
        art.cache_album_art(album, db)
    fresh_track_count = len(album.tracks) if getattr(album, "tracks", None) is not None else 0
    if not found_detail and fresh_track_count == 0:
        return {"ok": False, "reason": "no_release_match"}
    if found_detail and fresh_track_count == 0:
        return {"ok": False, "reason": "no_tracklist_found"}
    return {
        "ok": True,
        "rg": album.release_group_mb_id,
        "release_type": album.release_group_type,
        "cover": album.cover_path,
        "track_count": fresh_track_count,
        "total_track_count": effective_album_total_tracks(album),
    }


def bulk_enrich(SessionFactory, batch_size: int = BULK_ENRICH_BATCH_SIZE):
    """Background task: enrich a bounded batch of unresolved artists/albums."""
    progress["running"] = True
    progress["done"] = 0
    progress["error"] = None
    db = SessionFactory()
    try:
        retry_before = datetime.utcnow() - timedelta(hours=ENRICH_RETRY_COOLDOWN_HOURS)
        all_artists = [
            artist
            for artist in db.query(Artist).filter(
                ((Artist.mb_id.is_(None)) | (Artist.internet_release_total.is_(None)) | (Artist.internet_track_total.is_(None)))
                & ((Artist.internet_synced_at.is_(None)) | (Artist.internet_synced_at < retry_before))
            ).order_by(Artist.internet_synced_at.asc().nullsfirst(), Artist.id.asc()).all()
            if not is_various_artists_name(artist.name)
        ]
        all_albums = [
            album
            for album in db.query(Album).filter(Album.mb_id.is_(None), Album.release_group_mb_id.is_(None)).all()
            if album.artist is None or not is_various_artists_name(album.artist.name)
        ]
        artists = all_artists[:batch_size]
        album_slots = max(0, batch_size - len(artists))
        albums = all_albums[:album_slots]
        progress["total"] = len(artists) + len(albums)
        progress["remaining"] = max(0, len(all_artists) + len(all_albums) - progress["total"])
        if progress["total"] == 0:
            progress["current"] = "nothing queued"
            return
        for a in artists:
            progress["current"] = f"Artist: {a.name}"
            print(f"[enrich] {progress['current']} ({progress['done']}/{progress['total']})")
            try:
                result = enrich_artist(db, a)
                if not result.get("ok"):
                    a.internet_synced_at = datetime.utcnow()
                    db.commit()
            except Exception as e:
                print(f"[enrich] artist {a.name} failed: {e}")
                db.rollback()
                try:
                    a.internet_synced_at = datetime.utcnow()
                    db.commit()
                except Exception:
                    db.rollback()
            progress["done"] += 1
        for al in albums:
            progress["current"] = f"Album: {al.title}"
            print(f"[enrich] {progress['current']} ({progress['done']}/{progress['total']})")
            try:
                enrich_album(db, al)
            except Exception as e:
                print(f"[enrich] album {al.title} failed: {e}")
                db.rollback()
            progress["done"] += 1
        progress["current"] = "done"
    except Exception as e:
        progress["error"] = str(e)
        print(f"[enrich] bulk failed: {e}")
    finally:
        progress["running"] = False
        db.close()
