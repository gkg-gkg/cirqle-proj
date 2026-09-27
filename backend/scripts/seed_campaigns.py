"""Seed the campaign catalog with the original 12 hardcoded deals (Phase 3).

Copies the text fields that used to live in deal.html / browse.html into the
`campaign` table. Images start empty — add real photos per campaign through the
admin form afterwards (the emoji renders as a fallback until then).

Idempotent: does nothing if the table already has rows. Run from backend/:
    python scripts/seed_campaigns.py
"""
import json
import sys
from pathlib import Path

# Make `app` importable no matter how this script is launched, and read .env so
# it writes to the same DB the app uses (SQLite locally, RDS if DATABASE_URL set).
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

from sqlmodel import Session, select  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import Campaign  # noqa: E402

# brand, title, cardTitle, cardDesc, longDesc, emoji, category, rate, earn,
# spendDesc, totalPaid, members, claims, expiry, location, brandUrl, tags, terms
#
# totalPaid/members/claims are the social-proof fields browse.html and deal.html
# print verbatim. They ship at zero and stay there until something real counts
# them — a seeded number is a number we'd be inventing about our own members.
DEALS = [
    dict(brand="Nike", title="Nike Summer Sale — up to 13% back",
         card_title="Nike Summer Sale",
         card_desc="Cashback on all Nike footwear, apparel and accessories. Air Max, Jordan and more.",
         long_desc="Shop the biggest sale of the year and earn cashback on every purchase. From running shoes to lifestyle trainers, earn back on every pair.",
         emoji="👟", category="Fashion & Beauty", rate=13, earn="£13.00",
         spend_desc="on a £100 spend", total_paid="£0 paid to members",
         members="0", claims=0, expiry="30 Jun 2026", location="Online · UK",
         brand_url="https://www.nike.com/gb",
         tags=["Fashion & Beauty", "Footwear", "Sale", "New arrivals eligible"],
         terms='• Cashback on full-price & sale items<br>• Excludes gift cards<br>• Minimum spend: £20<br>• Post within 7 days of purchase<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="ASOS", title="ASOS New In — 10% cashback",
         card_title="ASOS New In",
         card_desc="10% back on thousands of styles. Fashion, beauty and accessories all included.",
         long_desc="Shop ASOS and earn cashback on thousands of styles. Fashion, beauty, accessories — earn every time.",
         emoji="👗", category="Fashion & Beauty", rate=10, earn="£6.00",
         spend_desc="on a £60 spend", total_paid="£0 paid to members",
         members="0", claims=0, expiry="Ongoing", location="Online · UK",
         brand_url="https://www.asos.com",
         tags=["Fashion & Beauty", "Clothing", "Accessories"],
         terms='• All categories eligible<br>• Excludes marketplace sellers<br>• No minimum spend<br>• Post within 7 days of purchase<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="Amazon", title="Amazon Prime — New account offer",
         card_title="Amazon Prime",
         card_desc="10% cashback on your first Prime subscription, plus cashback on eligible purchases.",
         long_desc="10% cashback on your first Amazon Prime subscription. Plus cashback on all eligible purchases through Cirqle.",
         emoji="📦", category="Electronics", rate=10, earn="£1.00",
         spend_desc="on first month", total_paid="£0 paid to members",
         members="0", claims=0, expiry="New members only", location="Online · UK",
         brand_url="https://www.amazon.co.uk",
         tags=["Electronics", "Subscription", "New members"],
         terms='• New Prime subscribers only<br>• First month eligible<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="Trainline", title="Trainline — 12% cashback on journeys",
         card_title="Trainline special offers",
         card_desc="Up to 12% cashback on trains, coaches and more at the best prices in the UK.",
         long_desc="Book trains, coaches and more through Cirqle and earn 12% cashback. Fast, easy, and fully tracked.",
         emoji="🚆", category="Travel", rate=12, earn="£5.40",
         spend_desc="on a £45 booking", total_paid="£0 paid to members",
         members="0", claims=0, expiry="31 Jul 2026", location="Online · UK",
         brand_url="https://www.thetrainline.com",
         tags=["Travel", "Trains", "Coaches", "UK"],
         terms='• All Trainline bookings eligible<br>• Excludes season tickets<br>• Post within 7 days of travel<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="Boots", title="Boots Beauty — 15% cashback",
         card_title="Boots Beauty",
         card_desc="15% back on beauty, skincare and health products. Advantage Card points on top.",
         long_desc="Earn 15% cashback on beauty, skincare, and health products at Boots. Advantage Card points on top.",
         emoji="💄", category="Fashion & Beauty", rate=15, earn="£5.25",
         spend_desc="on a £35 spend", total_paid="£0 paid to members",
         members="0", claims=0, expiry="Ongoing", location="In-store & online",
         brand_url="https://www.boots.com",
         tags=["Fashion & Beauty", "Health", "Skincare"],
         terms='• In-store & online eligible<br>• Excludes prescriptions<br>• Post within 7 days<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="Gymshark", title="Gymshark Sale — up to 20% cashback",
         card_title="Gymshark Sale",
         card_desc="Stack cashback on top of sale prices. Activewear, gym gear and accessories.",
         long_desc="The best cashback rate on the platform. Shop Gymshark, post your haul, and earn 20% back. Activewear, gym gear and accessories.",
         emoji="🏋️", category="Fitness", rate=20, earn="£14.00",
         spend_desc="on a £70 spend", total_paid="£0 paid to members",
         members="0", claims=0, expiry="15 Jul 2026", location="Online · UK",
         brand_url="https://www.gymshark.com",
         tags=["Fitness", "Activewear", "Gymwear", "Sale"],
         terms='• All items eligible<br>• Post within 5 days<br>• Minimum spend: £40<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="Deliveroo", title="Deliveroo — 8% back on every order",
         card_title="Deliveroo",
         card_desc="Cashback on food, grocery and drink deliveries from thousands of restaurants.",
         long_desc="Get cashback on food, grocery and alcohol deliveries from thousands of restaurants and shops across the UK.",
         emoji="🍔", category="Food & Drink", rate=8, earn="£2.00",
         spend_desc="on a £25 order", total_paid="£0 paid to members",
         members="0", claims=0, expiry="Ongoing", location="App & online",
         brand_url="https://deliveroo.co.uk",
         tags=["Food & Drink", "Takeaway", "Groceries"],
         terms='• All orders eligible<br>• Minimum order: £10<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="Currys", title="Currys Tech Deals — 11% cashback",
         card_title="Currys Tech Deals",
         card_desc="Cashback on laptops, TVs, appliances and smart home from the UK's biggest tech retailer.",
         long_desc="Earn cashback on laptops, TVs, appliances and smart home devices. UK's biggest electronics retailer.",
         emoji="💻", category="Electronics", rate=11, earn="£16.50",
         spend_desc="on a £150 spend", total_paid="£0 paid to members",
         members="0", claims=0, expiry="20 Jul 2026", location="In-store & online",
         brand_url="https://www.currys.co.uk",
         tags=["Electronics", "Tech", "Home Appliances"],
         terms='• In-store & online eligible<br>• Excludes clearance<br>• Minimum spend: £50<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="TUI", title="TUI Holidays — 9% cashback",
         card_title="TUI Holidays",
         card_desc="Book your dream holiday and earn back. Packages, flights, hotels and cruises included.",
         long_desc="Book your dream holiday and earn cashback. Package holidays, flights, hotels and cruises all included.",
         emoji="✈️", category="Travel", rate=9, earn="£72.00",
         spend_desc="on a £800 holiday", total_paid="£0 paid to members",
         members="0", claims=0, expiry="1 Sep 2026", location="UK departures",
         brand_url="https://www.tui.co.uk",
         tags=["Travel", "Holidays", "Flights", "Hotels"],
         terms='• UK departures only<br>• Post must go live before departure<br>• Excludes last-minute deals<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="IKEA", title="IKEA Home — 7% cashback",
         card_title="IKEA Home",
         card_desc="Cashback on furniture, storage, kitchenware and décor. Free delivery over £50.",
         long_desc="Earn cashback on furniture, storage, kitchenware and home décor. Free delivery on orders over £50.",
         emoji="🛋️", category="Home & Living", rate=7, earn="£7.00",
         spend_desc="on a £100 spend", total_paid="£0 paid to members",
         members="0", claims=0, expiry="Ongoing", location="In-store & online",
         brand_url="https://www.ikea.com/gb/en/",
         tags=["Home & Living", "Furniture", "Decor"],
         terms='• All departments eligible<br>• Excludes food market<br>• Minimum spend: £30<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="Costa", title="Costa Coffee — 6% cashback",
         card_title="Costa Coffee",
         card_desc="Cashback on drinks, food and Costa Club purchases. In-store and on the app.",
         long_desc="Get cashback on drinks, food and Costa Club purchases. Works in-store and on the Costa app across the UK.",
         emoji="☕", category="Food & Drink", rate=6, earn="£0.90",
         spend_desc="on a £15 order", total_paid="£0 paid to members",
         members="0", claims=0, expiry="Ongoing", location="In-store · UK",
         brand_url="https://www.costa.co.uk",
         tags=["Food & Drink", "Coffee", "In-store"],
         terms='• In-store purchases only<br>• App orders eligible<br>• No minimum spend<br>• <a href="terms.html">Full terms →</a>'),
    dict(brand="PureGym", title="PureGym Membership — 14% back",
         card_title="PureGym Membership",
         card_desc="Cashback on new monthly or annual memberships. 300+ gyms across the UK.",
         long_desc="Earn cashback on new monthly or annual gym memberships at PureGym. 300+ gyms across the UK.",
         emoji="💪", category="Fitness", rate=14, earn="£2.80",
         spend_desc="per month", total_paid="£0 paid to members",
         members="0", claims=0, expiry="New members only", location="UK wide",
         brand_url="https://www.puregym.com",
         tags=["Fitness", "Gym", "Membership", "New members"],
         terms='• New members only<br>• Monthly & annual plans eligible<br>• Post within 7 days of joining<br>• <a href="terms.html">Full terms →</a>'),
]


def main() -> None:
    init_db()
    with Session(engine) as session:
        if session.exec(select(Campaign)).first():
            print("Campaigns already seeded — skipping.")
            return
        for d in DEALS:
            session.add(Campaign(tags=json.dumps(d.pop("tags")), images="[]", **d))
        session.commit()
        print(f"Seeded {len(DEALS)} campaigns.")


if __name__ == "__main__":
    main()
