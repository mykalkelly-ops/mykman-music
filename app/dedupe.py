from collections import defaultdict

from sqlalchemy.orm import Session

from .models import Artist, Album, Song, PlaylistSong, Comparison, Note, SongCredit, ArtistMembership


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _pick_song(songs: list[Song]) -> Song:
    return max(
        songs,
        key=lambda song: (
            song.comparison_count or 0,
            1 if song.liked else 0,
            song.id * -1,
        ),
    )


def _merge_song_into(db: Session, keep: Song, drop: Song) -> None:
    if keep.id == drop.id:
        return

    for row in db.query(PlaylistSong).filter(PlaylistSong.song_id == drop.id).all():
        exists = (
            db.query(PlaylistSong)
            .filter(PlaylistSong.playlist_id == row.playlist_id, PlaylistSong.song_id == keep.id)
            .first()
        )
        if exists:
            db.delete(row)
        else:
            row.song_id = keep.id

    for comp in db.query(Comparison).filter(
        (Comparison.song_a_id == drop.id) | (Comparison.song_b_id == drop.id) | (Comparison.winner_id == drop.id)
    ).all():
        if comp.song_a_id == drop.id:
            comp.song_a_id = keep.id
        if comp.song_b_id == drop.id:
            comp.song_b_id = keep.id
        if comp.winner_id == drop.id:
            comp.winner_id = keep.id
        if comp.song_a_id == comp.song_b_id:
            db.delete(comp)

    for note in db.query(Note).filter(Note.target_type == "song", Note.target_id == drop.id).all():
        note.target_id = keep.id

    for credit in db.query(SongCredit).filter(SongCredit.song_id == drop.id).all():
        exists = (
            db.query(SongCredit)
            .filter(
                SongCredit.song_id == keep.id,
                SongCredit.artist_id == credit.artist_id,
                SongCredit.role == credit.role,
            )
            .first()
        )
        if exists:
            db.delete(credit)
        else:
            credit.song_id = keep.id

    keep.liked = keep.liked or drop.liked
    keep.comparison_count = max(keep.comparison_count or 0, drop.comparison_count or 0)
    if not keep.apple_track_id and drop.apple_track_id:
        keep.apple_track_id = drop.apple_track_id
    db.delete(drop)


def _merge_album_into(db: Session, keep: Album, drop: Album) -> None:
    if keep.id == drop.id:
        return

    songs_by_title: dict[str, list[Song]] = defaultdict(list)
    for song in keep.songs:
        songs_by_title[_norm(song.title)].append(song)

    for song in list(drop.songs):
        key = _norm(song.title)
        if songs_by_title.get(key):
            keeper_song = _pick_song(songs_by_title[key])
            _merge_song_into(db, keeper_song, song)
        else:
            song.album_id = keep.id
            songs_by_title[key].append(song)

    for note in db.query(Note).filter(Note.target_type == "album", Note.target_id == drop.id).all():
        note.target_id = keep.id

    keep.confirmed_listened = keep.confirmed_listened or drop.confirmed_listened
    if not keep.year and drop.year:
        keep.year = drop.year
    if not keep.genre and drop.genre:
        keep.genre = drop.genre
    if not keep.cover_path and drop.cover_path:
        keep.cover_path = drop.cover_path
    if not keep.cover_url and drop.cover_url:
        keep.cover_url = drop.cover_url
    db.delete(drop)


def _merge_artist_into(db: Session, keep: Artist, drop: Artist) -> None:
    if keep.id == drop.id:
        return

    existing_albums = {_norm(album.title): album for album in keep.albums}
    for album in list(drop.albums):
        match = existing_albums.get(_norm(album.title))
        if match:
            _merge_album_into(db, match, album)
        else:
            album.artist_id = keep.id
            existing_albums[_norm(album.title)] = album

    for note in db.query(Note).filter(Note.target_type == "artist", Note.target_id == drop.id).all():
        note.target_id = keep.id

    for credit in db.query(SongCredit).filter(SongCredit.artist_id == drop.id).all():
        exists = (
            db.query(SongCredit)
            .filter(
                SongCredit.song_id == credit.song_id,
                SongCredit.artist_id == keep.id,
                SongCredit.role == credit.role,
            )
            .first()
        )
        if exists:
            db.delete(credit)
        else:
            credit.artist_id = keep.id

    for membership in db.query(ArtistMembership).filter(ArtistMembership.artist_id == drop.id).all():
        membership.artist_id = keep.id
    for membership in db.query(ArtistMembership).filter(ArtistMembership.child_artist_id == drop.id).all():
        membership.child_artist_id = keep.id

    keep.kind = keep.kind or drop.kind
    keep.mb_id = keep.mb_id or drop.mb_id
    keep.image_path = keep.image_path or drop.image_path
    keep.image_url = keep.image_url or drop.image_url
    keep.gender = keep.gender or drop.gender
    keep.is_band = keep.is_band if keep.is_band is not None else drop.is_band
    db.delete(drop)


def merge_case_duplicates(db: Session) -> dict[str, int]:
    merged_artists = 0
    merged_albums = 0
    merged_songs = 0

    artist_groups: dict[str, list[Artist]] = defaultdict(list)
    for artist in db.query(Artist).order_by(Artist.id.asc()).all():
        artist_groups[_norm(artist.name)].append(artist)

    for _, group in artist_groups.items():
        if len(group) < 2:
            continue
        keep = min(group, key=lambda artist: artist.id)
        for drop in group:
            if drop.id == keep.id:
                continue
            _merge_artist_into(db, keep, drop)
            merged_artists += 1

    db.flush()

    album_groups: dict[tuple[int, str], list[Album]] = defaultdict(list)
    for album in db.query(Album).order_by(Album.id.asc()).all():
        album_groups[(album.artist_id, _norm(album.title))].append(album)
    for _, group in album_groups.items():
        if len(group) < 2:
            continue
        keep = min(group, key=lambda album: album.id)
        for drop in group:
            if drop.id == keep.id:
                continue
            _merge_album_into(db, keep, drop)
            merged_albums += 1

    db.flush()

    song_groups: dict[tuple[int, str], list[Song]] = defaultdict(list)
    for song in db.query(Song).order_by(Song.id.asc()).all():
        song_groups[(song.album_id, _norm(song.title))].append(song)
    for _, group in song_groups.items():
        if len(group) < 2:
            continue
        keep = _pick_song(group)
        for drop in group:
            if drop.id == keep.id:
                continue
            _merge_song_into(db, keep, drop)
            merged_songs += 1

    db.commit()
    return {"artists": merged_artists, "albums": merged_albums, "songs": merged_songs}


def merge_artist_names(db: Session, keep_name: str, drop_name: str) -> bool:
    keep = db.query(Artist).filter(Artist.name.ilike(keep_name)).one_or_none()
    drop = db.query(Artist).filter(Artist.name.ilike(drop_name)).one_or_none()
    if keep is None or drop is None or keep.id == drop.id:
        return False
    _merge_artist_into(db, keep, drop)
    db.commit()
    return True
