"""How one merchant compares — to their category, and to the wider industry.

A number on its own tells a merchant nothing. "31% of your customers came back"
only becomes a decision once they know whether 31% is good, and the honest
answer depends on what kind of business they run.

Two reference points, in order of preference:

  • Cirqle's own median for merchants in the same category. This is the better
    comparison — same platform, same mechanics, same country — but it needs
    enough merchants in that category to mean anything, so it is withheld until
    there are (see customers.MIN_CATEGORY_MERCHANTS).

  • Published industry figures otherwise. These are US restaurant numbers and
    are directional only; everything that surfaces them says so.

Computed on read like everything else here. It is O(merchants), which is fine
at this size and is the first thing to move behind a cached rollup if it isn't.
"""
import json
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from .customers import (INDUSTRY, MIN_CATEGORY_MERCHANTS, build_customers,
                        repeat_rate, return_rate)
from .models import Merchant

# The figures we benchmark, and which way is better. Every one of these has to
# be a rate or a ratio rather than a total — comparing a small merchant's
# customer COUNT against a large one's would only ever tell them they are small.
MEASURES = ("repeatRate", "returnRate30", "claimsPerCustomer")


def _categories(merchant: Merchant) -> list[str]:
    try:
        val = json.loads(merchant.categories or "[]")
        return [str(c).strip().lower() for c in val if str(c).strip()]
    except (ValueError, TypeError):
        return []


def _figures(merchant_id: int, session: Session, today: date) -> Optional[dict]:
    """One merchant's benchmarkable rates, or None if they have no customers.

    A merchant with nothing to measure is left out of the median entirely rather
    than entered as zeroes, which would drag every comparison down as the
    platform signs up brands that haven't launched yet.
    """
    records = build_customers(merchant_id, session)
    if not records:
        return None
    claims = sum(c.claims for c in records)
    return {
        "repeatRate": repeat_rate(records, today),
        "returnRate30": return_rate(records, 30, today),
        "claimsPerCustomer": round(claims / len(records), 2),
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def compare(merchant: Merchant, session: Session,
            today: Optional[date] = None) -> dict:
    """This merchant's rates beside the best reference we can honestly give."""
    today = today or date.today()
    mine = _figures(merchant.id, session, today)
    if mine is None:
        return {"measures": [], "peerCategory": "", "peerCount": 0}

    my_cats = set(_categories(merchant))
    peers: list[dict] = []
    peer_category = ""
    if my_cats:
        others = session.exec(
            select(Merchant).where(Merchant.id != merchant.id)).all()
        matching = [m for m in others if my_cats & set(_categories(m))]
        for m in matching:
            figures = _figures(m.id, session, today)
            if figures is not None:
                peers.append(figures)
        peer_category = sorted(my_cats)[0]

    # Include this merchant in the count the threshold is judged on — they are
    # one of the brands in the category.
    enough = len(peers) + 1 >= MIN_CATEGORY_MERCHANTS

    measures = []
    for key in MEASURES:
        value = mine[key]
        peer_median = _median([p[key] for p in peers]) if enough and peers else None
        reference = INDUSTRY.get(key, {})
        measures.append({
            "measure": key,
            "value": value,
            # Cirqle's own median wins when we have one; the published figure is
            # the fallback, and `source` says which the merchant is looking at.
            "comparedTo": peer_median if peer_median is not None
            else reference.get("baseline", 0.0),
            "source": "cirqle" if peer_median is not None else "industry",
            "strong": reference.get("strong", 0.0),
            "ahead": (value >= peer_median if peer_median is not None
                      else value >= reference.get("baseline", 0.0)),
        })

    return {
        "measures": measures,
        "peerCategory": peer_category if enough else "",
        "peerCount": len(peers) if enough else 0,
    }
