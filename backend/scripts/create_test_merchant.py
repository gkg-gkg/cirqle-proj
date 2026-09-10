"""Create a ready-to-sign-in test merchant, for local testing only.

The real path (application -> admin approval -> emailed invite -> merchant
sets their own password) is the right one for production, but is a lot of
manual steps just to test the reference-receipt / Claude-vision verification
flow locally. This script skips straight to a usable account: a known
password, email already "verified", an active subscription (needed to pass
submit_campaign's billing gate), and idempotent (safe to re-run).

Run from backend/:
    python scripts/create_test_merchant.py
"""
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

from sqlmodel import Session, select  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import Merchant  # noqa: E402
from app.security import hash_password  # noqa: E402

EMAIL = "test-merchant@example.com"
PASSWORD = "test-password-123"


def main() -> None:
    init_db()
    with Session(engine) as session:
        merchant = session.exec(select(Merchant).where(Merchant.email == EMAIL)).first()
        if merchant is None:
            merchant = Merchant(email=EMAIL, business_name="Test Merchant")
            print(f"Creating {EMAIL}")
        else:
            print(f"{EMAIL} already exists — resetting it to a known-good state")

        merchant.password_hash = hash_password(PASSWORD)
        merchant.must_set_password = False
        merchant.email_verified_at = datetime.utcnow()
        merchant.subscription_status = "active"   # needed to pass submit_campaign's billing gate
        session.add(merchant)
        session.commit()

    print(f"\nSign in with:\n  email:    {EMAIL}\n  password: {PASSWORD}\n")
    print("POST /merchant/signin with that email/password to get a bearer token,")
    print("then use it to call POST /merchant/reference-receipt.")


if __name__ == "__main__":
    main()
