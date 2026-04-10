import os
from pathlib import Path


def data_dir() -> Path:
    raw = os.environ.get("MYKMAN_DATA_DIR")
    if raw:
        path = Path(raw)
    else:
        path = Path(__file__).resolve().parent.parent / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path
