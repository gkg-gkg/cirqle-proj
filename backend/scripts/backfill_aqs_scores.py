"""Backfill Account Quality Scores for existing users (Phase 2).

Computes app.aqs.compute_aqs() for every user with at least 3 tagged posts on
file (per the spec's recommended minimum for a meaningful score) and
upserts the result into UserTrustScore. Idempotent/re-runnable — safe to use
as a manual recompute tool later too.

Run from backend/ after pulling:

    python scripts/backfill_aqs_scores.py
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

import json  # noqa: E402
from datetime import datetime  # noqa: E402

from sqlmodel import Session, func, select  # noqa: E402

from app.aqs import compute_aqs  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import Mention, User, UserTrustScore  # noqa: E402

MIN_POSTS = 3


def main() -> None:
    init_db()
    with Session(engine) as session:
        user_ids = session.exec(
            select(Mention.user_id)
            .group_by(Mention.user_id)
            .having(func.count(Mention.id) >= MIN_POSTS)
        ).all()

        updated = 0
        for user_id in user_ids:
            user = session.get(User, user_id)
            if user is None:
                continue
            mentions = session.exec(select(Mention).where(Mention.user_id == user_id)).all()
            result = compute_aqs(user, mentions)

            row = session.exec(
                select(UserTrustScore).where(UserTrustScore.user_id == user_id)
            ).first()
            if row is None:
                row = UserTrustScore(user_id=user_id, aqs_score=result.score)
            row.aqs_score = result.score
            row.aqs_computed_at = datetime.utcnow()
            row.aqs_inputs_snapshot = json.dumps(result.inputs_snapshot)
            session.add(row)
            updated += 1

        session.commit()
    print(f"Backfilled AQS for {updated} user(s) with >= {MIN_POSTS} tagged posts.")


if __name__ == "__main__":
    main()
