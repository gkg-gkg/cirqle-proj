"""Create (or update) the membership Products + Prices in Stripe.

Run this once per Stripe environment — and again whenever a price changes in
`app/payments.py`:

    python scripts/setup_stripe_plans.py            # uses STRIPE_SECRET_KEY from .env

It reads the TIERS dict in payments.py — the single source of truth — and makes
Stripe match it. Idempotent and safe to re-run:

  • the Product is created once, then reused (found by its lookup key)
  • a Price is immutable in Stripe, so changing a fee creates a NEW price and
    retires the old one's lookup key. Existing subscribers keep paying the old
    price until they switch plans; new signups get the new one.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

import os  # noqa: E402

import stripe  # noqa: E402

from app.payments import CURRENCY, TIERS  # noqa: E402


def main() -> None:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        sys.exit("STRIPE_SECRET_KEY is not set (backend/.env).")
    stripe.api_key = key
    mode = "TEST" if key.startswith("sk_test") else "LIVE"
    print(f"Stripe mode: {mode}")

    for tier_key, tier in TIERS.items():
        lookup = tier["lookup_key"]
        pence = int(round(tier["fee"] * 100))

        existing = stripe.Price.list(lookup_keys=[lookup], active=True,
                                     limit=1).to_dict()["data"]
        if existing:
            price = existing[0]
            if (price["unit_amount"] == pence
                    and price["currency"] == CURRENCY
                    and (price.get("recurring") or {}).get("interval") == "month"):
                print(f"  = {tier['name']:8} £{tier['fee']:>6.2f}/mo  unchanged ({price['id']})")
                continue
            # Price objects are immutable: free the lookup key, then re-create.
            stripe.Price.modify(price["id"], lookup_key=f"{lookup}_retired_{price['id'][-8:]}",
                                active=False)
            product_id = price["product"]
            print(f"  ~ {tier['name']:8} price changed — retiring {price['id']}")
        else:
            product = stripe.Product.create(
                name=f"Cirqle {tier['name']}",
                description=(f"{tier['blurb']} Includes £{tier['allowance']:.0f} of "
                             f"fee-free cashback top-ups each month."),
                metadata={"tier": tier_key},
            )
            product_id = product.id
            print(f"  + {tier['name']:8} product created ({product_id})")

        price = stripe.Price.create(
            product=product_id,
            currency=CURRENCY,
            unit_amount=pence,
            recurring={"interval": "month"},
            lookup_key=lookup,
            transfer_lookup_key=True,
            metadata={"tier": tier_key, "allowance": str(tier["allowance"])},
        )
        print(f"  + {tier['name']:8} £{tier['fee']:>6.2f}/mo  price {price.id}")

    print("Done — plans are in sync with app/payments.py TIERS.")


if __name__ == "__main__":
    main()
