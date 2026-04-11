from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, ForeignKey, DateTime, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

# Default Glicko-2 starting values
DEFAULT_RATING = 1500.0
DEFAULT_RD = 350.0
DEFAULT_VOL = 0.06


class Artist(Base):
    __tablename__ = "artists"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False, index=True)
    gender = Column(String, nullable=True)  # legacy: M / F / NB / Band / Unknown
    is_band = Column(Boolean, nullable=True)  # legacy
    country = Column(String, nullable=True)
    mb_id = Column(String, nullable=True)
    kind = Column(String, nullable=True, default="solo")  # 'solo' | 'group' | 'collab'
    image_url = Column(String, nullable=True)
    image_path = Column(String, nullable=True)
    disambiguation = Column(String, nullable=True)
    start_year = Column(Integer, nullable=True)
    end_year = Column(Integer, nullable=True)
    albums = relationship("Album", back_populates="artist", cascade="all, delete-orphan")


class Person(Base):
    __tablename__ = "persons"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False, index=True)
    gender = Column(String, nullable=False, default="unknown")  # 'male'|'female'|'nonbinary'|'unknown'
    country = Column(String, nullable=True)
    birth_year = Column(Integer, nullable=True)
    notes = Column(String, nullable=True)
    mb_id = Column(String, nullable=True)


class ArtistMembership(Base):
    __tablename__ = "artist_memberships"
    id = Column(Integer, primary_key=True)
    artist_id = Column(Integer, ForeignKey("artists.id"), nullable=False, index=True)
    # Exactly one of person_id / child_artist_id should be set (not enforced).
    person_id = Column(Integer, ForeignKey("persons.id"), nullable=True, index=True)
    child_artist_id = Column(Integer, ForeignKey("artists.id"), nullable=True, index=True)
    role = Column(String, nullable=False, default="member")  # 'member'|'frontperson'|'producer'|'guest'
    start_year = Column(Integer, nullable=True)
    end_year = Column(Integer, nullable=True)


class SongCredit(Base):
    __tablename__ = "song_credits"
    id = Column(Integer, primary_key=True)
    song_id = Column(Integer, ForeignKey("songs.id"), nullable=False, index=True)
    artist_id = Column(Integer, ForeignKey("artists.id"), nullable=False, index=True)
    role = Column(String, nullable=False, default="primary")  # 'primary'|'featured'|'producer'
    __table_args__ = (UniqueConstraint("song_id", "artist_id", "role", name="uq_song_credit"),)


class Album(Base):
    __tablename__ = "albums"
    id = Column(Integer, primary_key=True)
    artist_id = Column(Integer, ForeignKey("artists.id"), nullable=False, index=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=True)
    genre = Column(String, nullable=True)
    total_track_count = Column(Integer, nullable=True)
    # "listened" is implied if any of its songs are in a playlist.
    # "confirmed_listened" is for user-confirmed albums with no playlist songs.
    confirmed_listened = Column(Boolean, default=False)
    excluded_from_listened = Column(Boolean, default=False)
    mb_id = Column(String, nullable=True)
    release_group_mb_id = Column(String, nullable=True)
    cover_url = Column(String, nullable=True)
    cover_path = Column(String, nullable=True)
    artist = relationship("Artist", back_populates="albums")
    songs = relationship("Song", back_populates="album", cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("artist_id", "title", name="uq_album_artist_title"),)


class Song(Base):
    __tablename__ = "songs"
    id = Column(Integer, primary_key=True)
    album_id = Column(Integer, ForeignKey("albums.id"), nullable=False, index=True)
    title = Column(String, nullable=False)
    duration_ms = Column(Integer, nullable=True)
    apple_track_id = Column(String, nullable=True, index=True)
    glicko_rating = Column(Float, default=DEFAULT_RATING)
    glicko_rd = Column(Float, default=DEFAULT_RD)
    glicko_vol = Column(Float, default=DEFAULT_VOL)
    comparison_count = Column(Integer, default=0)
    # Binary-search placement state (efficient cold-start for new songs)
    placement_pending = Column(Boolean, default=True)
    placement_lo = Column(Float, nullable=True)  # lower bound on true rating
    placement_hi = Column(Float, nullable=True)  # upper bound on true rating
    liked = Column(Boolean, default=False, nullable=False)
    album = relationship("Album", back_populates="songs")
    playlist_entries = relationship("PlaylistSong", back_populates="song", cascade="all, delete-orphan")


class SongLink(Base):
    __tablename__ = "song_links"
    id = Column(Integer, primary_key=True)
    left_song_id = Column(Integer, ForeignKey("songs.id"), nullable=False, index=True)
    right_song_id = Column(Integer, ForeignKey("songs.id"), nullable=False, index=True)
    relation = Column(String, nullable=False, default="same_song")  # currently same_song
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("left_song_id", "right_song_id", "relation", name="uq_song_link"),)


class Playlist(Base):
    __tablename__ = "playlists"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    month = Column(Integer, nullable=True)
    year = Column(Integer, nullable=True)
    songs = relationship("PlaylistSong", back_populates="playlist", cascade="all, delete-orphan")


class PlaylistSong(Base):
    __tablename__ = "playlist_songs"
    id = Column(Integer, primary_key=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id"), nullable=False, index=True)
    song_id = Column(Integer, ForeignKey("songs.id"), nullable=False, index=True)
    added_at = Column(DateTime, nullable=True)
    playlist = relationship("Playlist", back_populates="songs")
    song = relationship("Song", back_populates="playlist_entries")
    __table_args__ = (UniqueConstraint("playlist_id", "song_id", name="uq_playlist_song"),)


class Comment(Base):
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True)
    note_id = Column(Integer, ForeignKey("notes.id"), nullable=False, index=True)
    author_name = Column(String, nullable=False, default="Anonymous")
    body = Column(String, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    approved = Column(Boolean, default=False)


class Note(Base):
    __tablename__ = "notes"
    id = Column(Integer, primary_key=True)
    target_type = Column(String, nullable=False)  # "song" | "album" | "artist" | "general"
    target_id = Column(Integer, nullable=True)    # nullable for "general" updates
    title = Column(String, nullable=True)
    body = Column(String, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    visibility = Column(String, nullable=False, default="public")  # 'public' | 'subscribers'
    status = Column(String, nullable=False, default="published")  # 'draft' | 'published'
    kind = Column(String, nullable=False, default="essay")  # 'essay'|'review'|'fragment'|'note'|'update'


class Subscriber(Base):
    __tablename__ = "subscribers"
    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=True, index=True)
    access_code = Column(String, unique=True, nullable=False, index=True)
    tier = Column(String, nullable=False, default="supporter")
    status = Column(String, nullable=False, default="active")  # 'active'|'expired'|'revoked'
    kofi_transaction_id = Column(String, unique=True, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    notes = Column(String, nullable=True)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id = Column(Integer, primary_key=True)
    token = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)


class Comparison(Base):
    __tablename__ = "comparisons"
    id = Column(Integer, primary_key=True)
    song_a_id = Column(Integer, ForeignKey("songs.id"), nullable=False)
    song_b_id = Column(Integer, ForeignKey("songs.id"), nullable=False)
    winner_id = Column(Integer, ForeignKey("songs.id"), nullable=True)  # null = skip/tie
    difficulty = Column(String, nullable=True)  # 'easy' | 'hard' | null
    nostalgia = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db(engine):
    Base.metadata.create_all(engine)
    # Lightweight SQLite migration: add columns if missing.
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    existing_cols = {c["name"] for c in insp.get_columns("songs")}
    with engine.begin() as conn:
        if "placement_pending" not in existing_cols:
            conn.execute(text("ALTER TABLE songs ADD COLUMN placement_pending BOOLEAN DEFAULT 1"))
        if "placement_lo" not in existing_cols:
            conn.execute(text("ALTER TABLE songs ADD COLUMN placement_lo FLOAT"))
        if "placement_hi" not in existing_cols:
            conn.execute(text("ALTER TABLE songs ADD COLUMN placement_hi FLOAT"))
        if "liked" not in existing_cols:
            conn.execute(text("ALTER TABLE songs ADD COLUMN liked BOOLEAN DEFAULT 0"))
        # Add artists.kind if missing
        try:
            artist_cols = {c["name"] for c in insp.get_columns("artists")}
            if "kind" not in artist_cols:
                conn.execute(text("ALTER TABLE artists ADD COLUMN kind VARCHAR"))
            for col, ddl in [
                ("image_url", "ALTER TABLE artists ADD COLUMN image_url VARCHAR"),
                ("image_path", "ALTER TABLE artists ADD COLUMN image_path VARCHAR"),
                ("disambiguation", "ALTER TABLE artists ADD COLUMN disambiguation VARCHAR"),
                ("start_year", "ALTER TABLE artists ADD COLUMN start_year INTEGER"),
                ("end_year", "ALTER TABLE artists ADD COLUMN end_year INTEGER"),
            ]:
                if col not in artist_cols:
                    try:
                        conn.execute(text(ddl))
                    except Exception:
                        pass
        except Exception:
            pass
        # Album enrichment columns
        try:
            album_cols = {c["name"] for c in insp.get_columns("albums")}
            for col, ddl in [
                ("mb_id", "ALTER TABLE albums ADD COLUMN mb_id VARCHAR"),
                ("release_group_mb_id", "ALTER TABLE albums ADD COLUMN release_group_mb_id VARCHAR"),
                ("cover_url", "ALTER TABLE albums ADD COLUMN cover_url VARCHAR"),
                ("cover_path", "ALTER TABLE albums ADD COLUMN cover_path VARCHAR"),
                ("total_track_count", "ALTER TABLE albums ADD COLUMN total_track_count INTEGER"),
                ("excluded_from_listened", "ALTER TABLE albums ADD COLUMN excluded_from_listened BOOLEAN DEFAULT 0"),
            ]:
                if col not in album_cols:
                    try:
                        conn.execute(text(ddl))
                    except Exception:
                        pass
        except Exception:
            pass
        # Note.visibility
        try:
            note_cols = {c["name"] for c in insp.get_columns("notes")}
            if "visibility" not in note_cols:
                try:
                    conn.execute(text("ALTER TABLE notes ADD COLUMN visibility VARCHAR DEFAULT 'public'"))
                except Exception:
                    pass
                note_cols.add("visibility")
            if "status" not in note_cols:
                try:
                    conn.execute(text("ALTER TABLE notes ADD COLUMN status VARCHAR DEFAULT 'published'"))
                except Exception:
                    pass
            if "kind" not in note_cols:
                try:
                    conn.execute(text("ALTER TABLE notes ADD COLUMN kind VARCHAR DEFAULT 'essay'"))
                except Exception:
                    pass
        except Exception:
            pass
        # Person.mb_id
        try:
            person_cols = {c["name"] for c in insp.get_columns("persons")}
            if "mb_id" not in person_cols:
                try:
                    conn.execute(text("ALTER TABLE persons ADD COLUMN mb_id VARCHAR"))
                except Exception:
                    pass
        except Exception:
            pass
        # Comparison metadata
        try:
            comparison_cols = {c["name"] for c in insp.get_columns("comparisons")}
            if "difficulty" not in comparison_cols:
                conn.execute(text("ALTER TABLE comparisons ADD COLUMN difficulty VARCHAR"))
            if "nostalgia" not in comparison_cols:
                conn.execute(text("ALTER TABLE comparisons ADD COLUMN nostalgia BOOLEAN DEFAULT 0"))
        except Exception:
            pass
        try:
            insp.get_columns("admin_sessions")
        except Exception:
            try:
                AdminSession.__table__.create(bind=conn)
            except Exception:
                pass
        try:
            insp.get_columns("song_links")
        except Exception:
            pass
    # notes table is created by create_all above if missing
