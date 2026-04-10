import json
import shutil
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


def append_event(payload: dict) -> None:
    ensure_backup_dir()
    payload = dict(payload)
    payload["recorded_at"] = datetime.utcnow().isoformat()
    with JOURNAL_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def backup_before_import() -> str | None:
    return snapshot_db("pre-import")
