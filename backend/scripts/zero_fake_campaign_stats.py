"""Reset the invented social-proof numbers on every campaign to zero.

`total_paid`, `members` and `claims` are display-only strings/ints that nothing
in the app ever increments — they are whatever was typed into the seed script or
the admin form. The seeded catalogue shipped with invented figures ("£112,705
paid to members", "5.6k", 1840 claims; Nike and Trainline even carried the same
total), which is a claim about our own members that was never true.

This sets all three to zero everywhere. Zero is the correct number today, and it
stays correct until something actually counts these — at which point the counter
should write them, not a person. Safe to run more than once, and works on both
SQLite (local) and PostgreSQL (RDS). Run from backend/ after pulling:

    python scripts/zero_fake_campaign_stats.py
    python scripts/zero_fake_campaign_stats.py --dry-run
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Campaign  # noqa: E402

ZERO_TOTAL_PAID = "£0 paid to members"
ZERO_MEMBERS = "0"


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    with Session(engine) as session:
        changed = 0
        for c in session.exec(select(Campaign)).all():
            if (c.total_paid == ZERO_TOTAL_PAID and c.members == ZERO_MEMBERS
                    and c.claims == 0):
                continue
            print(f"  #{c.id} {c.brand}: "
                  f"total_paid={c.total_paid!r} members={c.members!r} claims={c.claims}"
                  f"  ->  {ZERO_TOTAL_PAID!r} / {ZERO_MEMBERS!r} / 0")
            c.total_paid = ZERO_TOTAL_PAID
            c.members = ZERO_MEMBERS
            c.claims = 0
            session.add(c)
            changed += 1
        if dry_run:
            print(f"\n[dry run] {changed} campaign(s) would be reset — nothing written.")
            return
        session.commit()
        print(f"\nReset {changed} campaign(s).")


if __name__ == "__main__":
    main()
