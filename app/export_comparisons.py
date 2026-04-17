"""Export the current DB's comparisons to data/backups.

Usage:
    python -m app.export_comparisons
"""
from .db import SessionLocal
from .history import export_comparisons_from_db
from .models import init_db
from .db import engine


def main() -> None:
    init_db(engine)
    db = SessionLocal()
    try:
        path = export_comparisons_from_db(db, "cli")
        print(f"Exported comparisons to {path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
