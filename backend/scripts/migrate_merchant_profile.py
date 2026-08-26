"""Add the merchant brand-profile columns (Phase 6b).

`SQLModel.create_all` creates NEW tables (so the new `merchanttransaction`
table appears automatically on the next setup.sh) but it never ALTERs an
existing table, so the columns added to `Merchant` must be applied by hand.

This script adds them idempotently — safe to run more than once, and works on
both SQLite (local) and PostgreSQL (RDS). Run from backend/ after pulling:

    python scripts/migrate_merchant_profile.py
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

from sqlalchemy import text  # noqa: E402

from app import models  # noqa: E402,F401 — registers every table so create_all builds them
from app.db import engine, init_db  # noqa: E402

# column name -> SQL default (all are TEXT-ish string columns)
NEW_COLUMNS = {
    "bio": "''",
    "categories": "'[]'",
    "website": "''",
    "instagram": "''",
    "tiktok": "''",
    "youtube": "''",
    "facebook": "''",
    "tips": "''",
    "logo_url": "''",
}


def _existing_columns(conn) -> set[str]:
    """Column names already on the `merchant` table, dialect-agnostic."""
    from sqlalchemy import inspect
    return {c["name"] for c in inspect(conn).get_columns("merchant")}


def main() -> None:
    # Ensure the table (and the new merchanttransaction table) exist first.
    init_db()
    with engine.begin() as conn:
        have = _existing_columns(conn)
        added = []
        for name, default in NEW_COLUMNS.items():
            if name in have:
                continue
            conn.execute(text(
                f"ALTER TABLE merchant ADD COLUMN {name} VARCHAR DEFAULT {default}"
            ))
            added.append(name)
    print("Added columns:", ", ".join(added) if added else "(none — already present)")


if __name__ == "__main__":
    main()
