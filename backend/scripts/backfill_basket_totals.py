"""Fill Receipt.basket_total / basket_currency / purchase_date from check_data.

Every receipt that has already been through the automated check is carrying the
numbers we need inside `check_data`. This copies them into the real columns the
d41f7b0c8e35 migration added, so the merchant dashboard can sum them.

It re-reads STORED JSON. Textract is not called, no receipt image is opened, and
nothing costs money. Safe to run repeatedly: each row is recomputed from its own
check_data, so a second run writes the same values.

    python scripts/backfill_basket_totals.py            # do it
    python scripts/backfill_basket_totals.py --dry-run  # just report

Rows whose check never ran, or errored, or whose total was illegible, are left
with NULL columns and counted as "no reading" — that is the honest state, and
the dashboard reports coverage from it.
"""
import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# Before app.db, which picks its engine from DATABASE_URL at import time. The
# API gets that from .env via main.py; a script that skips main.py has to load
# it itself or it silently falls back to a local SQLite file — which on the
# server is an empty one, so the run fails with "no such table: receipt"
# rather than touching production. Same as alembic/env.py and
# scripts/migrate_merchant_profile.py.
from dotenv import load_dotenv                              # noqa: E402
load_dotenv(BACKEND_DIR / ".env")

from sqlmodel import Session, select                        # noqa: E402

from app.db import engine                                   # noqa: E402
from app.models import Receipt                              # noqa: E402
from app.verify import apply_reading                        # noqa: E402


def reading_of(receipt: Receipt) -> dict:
    """The `reading` block from a receipt's check_data, or {} if there isn't one.

    An errored check stores {"error": ...} with no reading, and a receipt that
    was never checked stores "{}" — both come back empty here.
    """
    try:
        return json.loads(receipt.check_data or "{}").get("reading") or {}
    except ValueError:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    args = ap.parse_args()

    filled = no_reading = 0
    with Session(engine) as session:
        receipts = session.exec(select(Receipt)).all()
        for r in receipts:
            reading = reading_of(r)
            if not reading:
                no_reading += 1
                continue
            apply_reading(r, reading)
            if r.basket_total is None:
                # Checked, but the total was never legible.
                no_reading += 1
                continue
            filled += 1
            if not args.dry_run:
                session.add(r)
        if not args.dry_run:
            session.commit()

    total = len(receipts)
    coverage = round(filled / total * 100, 1) if total else 0.0
    verb = "would fill" if args.dry_run else "filled"
    print(f"{total} receipts · {verb} {filled} · {no_reading} with no legible "
          f"total · coverage {coverage}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
