"""Replace every real brand name in the catalogue with its category.

None of the brands in the catalogue (Nike, ASOS, Deliveroo, ...) is onboarded —
they were placeholder listings, and brands we pitch read them as partners. This
turns each deal into a made-up "brand" named after its category: every deal in a
category gets the same name, text, rate and photos, copied from one template
deal per category (picked for unbranded photos). The frontend treats
`brand == category` as "made-up brand" and badges it as such.

Nothing is deleted: the replaced deals' old photos stay in S3 (a few MB, no
real cost).

Safe to run more than once. Run from backend/ after pulling:

    python scripts/genericize_campaigns.py --dry-run
    python scripts/genericize_campaigns.py
"""
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Campaign  # noqa: E402

# category -> (template deal id, short card description)
CATEGORIES = {
    "Fashion & Beauty": (2, "Cashback on clothes, shoes, beauty and accessories."),
    "Food & Drink": (15, "Cashback on meals out, takeaway and coffee."),
    "Electronics": (8, "Cashback on headphones, laptops and gadgets."),
    "Travel": (9, "Cashback on holidays, hotels and trips."),
    "Fitness": (12, "Cashback on gym memberships, classes and kit."),
    "Entertainment": (25, "Cashback on gigs, cinema and live events."),
    "Kids & Baby": (27, "Cashback on baby clothes, toys and essentials."),
    "Pets": (28, "Cashback on pet food, toys and supplies."),
    "Home & Living": (10, "Cashback on furniture, bedding and homeware."),
}

TERMS = ('• Example terms. Each brand sets its own<br>'
         '• <a href="terms.html">Full terms →</a>')

# Copied from the template as-is so every deal in a category reads the same.
COPIED = ["emoji", "rate", "earn", "spend_desc", "expiry", "location", "bg",
          "images", "stores", "referrals_enabled", "referral_reward_referrer",
          "referral_reward_referee", "visits_enabled", "visit_earn"]


def target_fields(category: str, template: Campaign, card_desc: str) -> dict:
    fields = {f: getattr(template, f) for f in COPIED}
    fields.update(
        brand=category,
        title=f"{category} — {template.rate:g}% cashback",
        card_title=category,
        card_desc=card_desc,
        long_desc=(f"A made-up {category} brand. It shows how a deal will look "
                   "on Cirqle, at a rate the brand itself would set."),
        terms=TERMS,
        brand_url="",
        tags=json.dumps([category]),
    )
    return fields


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    with Session(engine) as session:
        campaigns = session.exec(select(Campaign)).all()
        by_id = {c.id: c for c in campaigns}

        changed = 0
        for c in sorted(campaigns, key=lambda c: c.id):
            if c.category not in CATEGORIES:
                print(f"  #{c.id} {c.brand}: category {c.category!r} not mapped, skipped")
                continue
            template_id, card_desc = CATEGORIES[c.category]
            fields = target_fields(c.category, by_id[template_id], card_desc)
            diff = [k for k, v in fields.items() if getattr(c, k) != v]
            if not diff:
                continue
            print(f"  #{c.id} {c.brand!r} -> {fields['brand']!r} ({', '.join(diff)})")
            if not dry_run:
                for k, v in fields.items():
                    setattr(c, k, v)
                session.add(c)
            changed += 1

        if dry_run:
            print(f"\n[dry run] {changed} campaign(s) would change. Nothing written.")
            return
        session.commit()
        print(f"\nUpdated {changed} campaign(s).")


if __name__ == "__main__":
    main()
