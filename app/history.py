import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from .db import engine
from .paths import data_dir

DATA_DIR = data_dir()
BACKUP_DIR = DATA_DIR / "backups"
JOURNAL_PATH = BACKUP_DIR / "comparison_history.jsonl"
DB_PATH = DATA_DIR / "music.db"


def ensure_backup_dir() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def snapshot_db(label: str) -> str | None:
    ensure_backup_dir()
    if not DB_PATH.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"music-{label}-{stamp}.db"
    shutil.copy2(DB_PATH, target)
    return str(target)


def comparison_count_in_db(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(str(path))
        try:
            row = con.execute("SELECT COUNT(*) FROM comparisons").fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            con.close()
    except Exception:
        return None


def export_comparisons_from_db(db: Session, label: str = "manual") -> str:
    """Export the actual comparisons table with song metadata.

    The journal is append-only and useful, but this is the source-of-truth
    snapshot for recovery if a DB ever needs to be rebuilt.
    """
    from .models import Album, Artist, Comparison, Song

    ensure_backup_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"comparisons-{label}-{stamp}.json"
    rows = (
        db.query(Comparison)
        .order_by(Comparison.id.asc())
        .all()
    )
    song_ids = {c.song_a_id for c in rows} | {c.song_b_id for c in rows} | {c.winner_id for c in rows if c.winner_id}
    songs = {}
    if song_ids:
        for song in db.query(Song).filter(Song.id.in_(song_ids)).all():
            album = song.album
            artist = album.artist if album else None
            songs[song.id] = {
                "id": song.id,
                "title": song.title,
                "album": album.title if album else None,
                "artist": artist.name if artist else None,
                "apple_track_id": song.apple_track_id,
            }
    payload = {
        "exported_at": datetime.utcnow().isoformat(),
        "comparison_count": len(rows),
        "comparisons": [
            {
                "id": c.id,
                "song_a_id": c.song_a_id,
                "song_b_id": c.song_b_id,
                "winner_id": c.winner_id,
                "difficulty": c.difficulty,
                "nostalgia": bool(c.nostalgia),
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "song_a": songs.get(c.song_a_id),
                "song_b": songs.get(c.song_b_id),
                "winner": songs.get(c.winner_id) if c.winner_id else None,
            }
            for c in rows
        ],
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return str(target)


def append_event(payload: dict) -> None:
    ensure_backup_dir()
    payload = dict(payload)
    payload["recorded_at"] = datetime.utcnow().isoformat()
    with JOURNAL_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def backup_before_import() -> str | None:
    backup = snapshot_db("pre-import")
    try:
        from .db import SessionLocal

        db = SessionLocal()
        try:
            export_comparisons_from_db(db, "pre-import")
        finally:
            db.close()
    except Exception:
        # DB snapshot is the critical safety artifact; comparison export is a
        # second belt-and-suspenders layer and should not block imports.
        pass
    return backup
