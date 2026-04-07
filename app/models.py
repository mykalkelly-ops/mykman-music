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
    gender = Column(String, nullable=True)  # M / F / NB / Band / Unknown
    is_band = Column(Boolean, nullable=True)
    country = Column(String, nullable=True)
    mb_id = Column(String, nullable=True)
    albums = relationship("Album", back_populates="artist", cascade="all, delete-orphan")


class Album(Base):
    __tablename__ = "albums"
    id = Column(Integer, primary_key=True)
    artist_id = Column(Integer, ForeignKey("artists.id"), nullable=False, index=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=True)
    genre = Column(String, nullable=True)
    # "listened" is implied if any of its songs are in a playlist.
    # "confirmed_listened" is for user-confirmed albums with no playlist songs.
    confirmed_listened = Column(Boolean, default=False)
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
    album = relationship("Album", back_populates="songs")
    playlist_entries = relationship("PlaylistSong", back_populates="song", cascade="all, delete-orphan")


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


class Comparison(Base):
    __tablename__ = "comparisons"
    id = Column(Integer, primary_key=True)
    song_a_id = Column(Integer, ForeignKey("songs.id"), nullable=False)
    song_b_id = Column(Integer, ForeignKey("songs.id"), nullable=False)
    winner_id = Column(Integer, ForeignKey("songs.id"), nullable=True)  # null = skip/tie
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db(engine):
    Base.metadata.create_all(engine)
