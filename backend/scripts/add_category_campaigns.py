"""Add deals in three new categories: Entertainment, Kids & Baby, Pets.

The homepage calculator, the carousel and the category mosaic all build their
category lists from whatever GET /campaigns returns — there is no list of
categories in the markup — so new calculator chips mean new catalogue rows.
Two deals per category, so each one has a real low-to-average range to show
rather than a single rate printed twice.

Posts to the live API rather than writing the DB directly, so it goes through
the same validation the admin form does. Idempotent: skips any brand that is
already in the catalogue.

Run from backend/ with the admin key in the environment:
    CIRQLE_ADMIN_KEY=... python scripts/add_category_campaigns.py
    CIRQLE_ADMIN_KEY=... python scripts/add_category_campaigns.py --dry-run
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import uuid

API = os.environ.get("CIRQLE_API", "https://api.cirqle.co.uk")
ADMIN_KEY = os.environ.get("CIRQLE_ADMIN_KEY", "")

# python.org builds on macOS ship without a system trust store, so urllib
# can't verify api.cirqle.co.uk's certificate unless it is pointed at one.
try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

TERMS_TAIL = '<br>• Post within 7 days of purchase<br>• <a href="terms.html">Full terms →</a>'

# These are brand-new listings, so the social-proof fields are left empty
# rather than given invented totals — browse.html and deal.html already render
# a row with no totalPaid/members. Images are empty too; the card falls back to
# the category icon until real photos are added through the admin form.
DEALS = [
    dict(brand="Cineworld", title="Cineworld — 12% back on tickets",
         cardTitle="Cineworld Tickets",
         cardDesc="12% back on standard and premium screenings, booked online or at the box office.",
         longDesc="Cashback on every ticket you book, from a Tuesday night standard to an IMAX opening weekend. Snacks bought on the same receipt count too.",
         category="Entertainment", rate=12, earn="£1.44",
         spendDesc="on a £12 ticket", expiry="Ongoing", location="UK cinemas & online",
         brandUrl="https://www.cineworld.co.uk",
         tags=["Entertainment", "Cinema", "Tickets"],
         terms="• Standard and premium screenings eligible<br>• Food and drink on the same receipt counts<br>• Excludes gift cards and Unlimited memberships" + TERMS_TAIL),
    dict(brand="Ticketmaster", title="Ticketmaster — 7% back on live events",
         cardTitle="Ticketmaster",
         cardDesc="7% back on gigs, comedy and theatre. Face value only, booking fees excluded.",
         longDesc="Earn on the tickets you were buying anyway. Covers music, comedy, theatre and sport across the UK.",
         category="Entertainment", rate=7, earn="£3.85",
         spendDesc="on a £55 booking", expiry="Ongoing", location="Online · UK",
         brandUrl="https://www.ticketmaster.co.uk",
         tags=["Entertainment", "Live music", "Theatre"],
         terms="• Cashback on face value only<br>• Excludes booking and delivery fees<br>• Excludes resale listings" + TERMS_TAIL),

    dict(brand="Smyths Toys", title="Smyths Toys — 9% back",
         cardTitle="Smyths Toys",
         cardDesc="9% back across toys, games and outdoor play. In store and online.",
         longDesc="Cashback on the toy run, the birthday present and the big-ticket Christmas one. Full-price and sale items both count.",
         category="Kids & Baby", rate=9, earn="£3.15",
         spendDesc="on a £35 spend", expiry="Ongoing", location="UK stores & online",
         brandUrl="https://www.smythstoys.com/uk",
         tags=["Kids & Baby", "Toys", "Games"],
         terms="• Full-price and sale items eligible<br>• Excludes gift cards<br>• No minimum spend" + TERMS_TAIL),
    dict(brand="JoJo Maman Bébé", title="JoJo Maman Bébé — 14% back",
         cardTitle="JoJo Maman Bébé",
         cardDesc="14% back on babywear, maternity and nursery. One of our highest rates.",
         longDesc="Earn on the things you buy most in the first two years — sleepsuits, prams, maternity wear and nursery kit.",
         category="Kids & Baby", rate=14, earn="£8.40",
         spendDesc="on a £60 spend", expiry="31 Dec 2026", location="UK stores & online",
         brandUrl="https://www.jojomamanbebe.co.uk",
         tags=["Kids & Baby", "Maternity", "Nursery"],
         terms="• All categories eligible<br>• Excludes gift cards and outlet clearance<br>• Minimum spend: £20" + TERMS_TAIL),

    dict(brand="Pets at Home", title="Pets at Home — 8% back",
         cardTitle="Pets at Home",
         cardDesc="8% back on food, toys and accessories. Subscriptions count every month.",
         longDesc="Cashback on the weekly food shop for the other member of the household, plus toys, bedding and accessories.",
         category="Pets", rate=8, earn="£2.40",
         spendDesc="on a £30 spend", expiry="Ongoing", location="UK stores & online",
         brandUrl="https://www.petsathome.com",
         tags=["Pets", "Pet food", "Accessories"],
         terms="• Repeat delivery orders count every month<br>• Excludes veterinary services and gift cards<br>• No minimum spend" + TERMS_TAIL),
    dict(brand="Lily's Kitchen", title="Lily's Kitchen — 13% back",
         cardTitle="Lily's Kitchen",
         cardDesc="13% back on natural dog and cat food, including subscription boxes.",
         longDesc="Earn on every delivery, not just the first. Subscription orders are eligible for as long as the deal is live.",
         category="Pets", rate=13, earn="£5.20",
         spendDesc="on a £40 spend", expiry="Ongoing", location="Online · UK",
         brandUrl="https://www.lilyskitchen.co.uk",
         tags=["Pets", "Pet food", "Subscription"],
         terms="• Subscription deliveries eligible every month<br>• Excludes gift cards<br>• Minimum spend: £15" + TERMS_TAIL),
]


def multipart(payload: dict) -> tuple[bytes, str]:
    """Build the one-field multipart body POST /campaigns expects (it reads
    `payload` as a Form field, with images as optional File parts)."""
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="payload"\r\n\r\n'
        f"{json.dumps(payload)}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    return body, f"multipart/form-data; boundary={boundary}"


def main() -> int:
    dry = "--dry-run" in sys.argv
    if not ADMIN_KEY and not dry:
        print("Set CIRQLE_ADMIN_KEY (it's CIRQLE_ADMIN_KEY in the server .env).")
        return 1

    with urllib.request.urlopen(f"{API}/campaigns", timeout=20, context=SSL_CTX) as r:
        existing = {c["brand"] for c in json.load(r)}

    added = 0
    for d in DEALS:
        if d["brand"] in existing:
            print(f"skip   {d['brand']} — already in the catalogue")
            continue
        if dry:
            print(f"would add  {d['brand']:<18} {d['category']:<14} {d['rate']}%")
            added += 1
            continue
        body, ctype = multipart(d)
        req = urllib.request.Request(
            f"{API}/campaigns", data=body, method="POST",
            headers={"Content-Type": ctype, "X-Admin-Key": ADMIN_KEY},
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                out = json.load(r)
            print(f"added  #{out['id']:<4} {out['brand']:<18} {out['category']:<14} {out['rate']}%")
            added += 1
        except urllib.error.HTTPError as exc:
            print(f"FAILED {d['brand']}: HTTP {exc.code} {exc.read().decode()[:200]}")
            return 1

    print(f"\n{added} deal(s) {'to add' if dry else 'added'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
