"""Phase A — the merchant's revenue figures.

Cashback is a flat per-campaign amount, so what a shopper SPENT can only come
off the receipt itself. These tests pin down that the money maths is right and,
just as importantly, that an unreadable receipt reads as missing rather than as
a £0 purchase.
"""
import json
from datetime import date, datetime

import pytest
from sqlmodel import Session

from app.models import (Campaign, Merchant, MerchantTransaction, Receipt,
                        ReferralReward, User)
from app.routers.merchant import _compute_stats
from app.verify import apply_reading


def _merchant(session: Session) -> Merchant:
    m = Merchant(email="shop@example.com", password_hash="x",
                 business_name="Test Shop")
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def _campaign(session: Session, merchant: Merchant, **kw) -> Campaign:
    c = Campaign(brand="Test Shop", card_title="Lunch deal",
                 merchant_id=merchant.id, **kw)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _user(session: Session, n: int) -> User:
    u = User(first_name="M", last_name=str(n), email=f"m{n}@example.com",
             password_hash="x", status="approved")
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _claim(session: Session, user: User, campaign: Campaign, *, status="confirmed",
           amount=5.0, total=None, currency="", bought=None) -> Receipt:
    r = Receipt(user_id=user.id, post_id=f"p{user.id}-{campaign.id}-{amount}-{total}",
                campaign_id=campaign.id, brand=campaign.brand, amount=amount,
                image_key="k", status=status, basket_total=total,
                basket_currency=currency, purchase_date=bought)
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


# ── The revenue sum ──────────────────────────────────────────────────────────

def test_revenue_sums_legible_totals_on_credited_claims(session):
    m = _merchant(session)
    c = _campaign(session, m)
    _claim(session, _user(session, 1), c, total=40.00, amount=5.0)
    _claim(session, _user(session, 2), c, total=60.00, amount=5.0)

    stats = _compute_stats(m, session)

    assert stats.revenue == 100.00
    assert stats.averageBasket == 50.00
    assert stats.revenueCoverage == 100.0
    assert stats.deals[0].revenue == 100.00


def test_unreadable_total_is_missing_not_zero(session):
    """The whole point of the coverage figure: a receipt we couldn't read must
    not drag the average basket down as though the shopper spent nothing."""
    m = _merchant(session)
    c = _campaign(session, m)
    _claim(session, _user(session, 1), c, total=40.00)
    _claim(session, _user(session, 2), c, total=None)      # illegible

    stats = _compute_stats(m, session)

    assert stats.revenue == 40.00
    assert stats.averageBasket == 40.00       # not 20.00
    assert stats.revenueCoverage == 50.0


def test_pending_and_rejected_claims_are_not_revenue(session):
    m = _merchant(session)
    c = _campaign(session, m)
    _claim(session, _user(session, 1), c, total=40.00, status="confirmed")
    _claim(session, _user(session, 2), c, total=99.00, status="pending")
    _claim(session, _user(session, 3), c, total=99.00, status="rejected")

    stats = _compute_stats(m, session)

    assert stats.revenue == 40.00
    # Coverage is measured against credited claims, so the two uncredited ones
    # neither add revenue nor count against us.
    assert stats.revenueCoverage == 100.0


def test_foreign_currency_is_excluded(session):
    m = _merchant(session)
    c = _campaign(session, m)
    _claim(session, _user(session, 1), c, total=40.00, currency="GBP")
    _claim(session, _user(session, 2), c, total=1000.00, currency="USD")

    stats = _compute_stats(m, session)

    assert stats.revenue == 40.00


def test_blank_currency_counts_as_pounds(session):
    """Most UK receipts print no currency code. Excluding them would throw away
    nearly every row."""
    m = _merchant(session)
    c = _campaign(session, m)
    _claim(session, _user(session, 1), c, total=40.00, currency="")

    assert _compute_stats(m, session).revenue == 40.00


# ── Cost, return and discount rate ───────────────────────────────────────────

def test_return_on_cashback_counts_every_cost(session):
    m = _merchant(session)
    c = _campaign(session, m)
    _claim(session, _user(session, 1), c, total=200.00, amount=10.0)
    session.add(ReferralReward(user_id=1, kind="referrer", receipt_id=1,
                               campaign_id=c.id, merchant_id=m.id, amount=1.0,
                               status="available"))
    session.add(MerchantTransaction(merchant_id=m.id, kind="platform_fee",
                                    amount=9.0))
    session.commit()

    stats = _compute_stats(m, session)

    assert stats.cashbackGiven == 10.0
    assert stats.programmeCost == 20.0            # 10 cashback + 1 bonus + 9 fee
    assert stats.returnOnCashback == 10.0         # £200 revenue / £20 spent
    assert stats.discountRate == 10.0             # £20 / £200


def test_no_cost_yet_does_not_divide_by_zero(session):
    m = _merchant(session)
    c = _campaign(session, m)
    _claim(session, _user(session, 1), c, total=50.00, amount=0.0)

    stats = _compute_stats(m, session)

    assert stats.programmeCost == 0.0
    assert stats.returnOnCashback == 0.0


def test_merchant_with_no_deals_is_all_zero(session):
    stats = _compute_stats(_merchant(session), session)

    assert stats.revenue == 0.0
    assert stats.revenueCoverage == 0.0
    assert stats.returnOnCashback == 0.0
    assert stats.deals == []


# ── apply_reading, which both the check and the backfill go through ──────────

@pytest.mark.parametrize("reading, expected", [
    ({"total": 42.6, "currency": "GBP", "purchase_date": "2026-06-03"},
     (42.6, "GBP", date(2026, 6, 3))),
    ({"total": None, "currency": "", "purchase_date": ""},
     (None, "", None)),
    # Textract gave us a date it couldn't normalise — better a gap than a guess.
    ({"total": 10.0, "currency": "", "purchase_date": "03/06/2026"},
     (10.0, "", None)),
    # `True` is an int in Python and must not become £1.00.
    ({"total": True, "currency": "", "purchase_date": ""},
     (None, "", None)),
])
def test_apply_reading(reading, expected):
    r = Receipt(user_id=1, post_id="p", image_key="k")
    apply_reading(r, reading)
    assert (r.basket_total, r.basket_currency, r.purchase_date) == expected


def test_backfill_fills_columns_from_stored_check_data(session):
    """The backfill's contract: a row that has been checked already carries
    everything the columns need, so no image is reopened and Textract is never
    called again."""
    from scripts.backfill_basket_totals import reading_of

    m = _merchant(session)
    c = _campaign(session, m)
    r = _claim(session, _user(session, 1), c, total=None)
    r.check_status = "ok"
    r.check_data = json.dumps({"reading": {"total": 33.5, "currency": "GBP",
                                           "purchase_date": "2026-05-01"},
                               "reasons": []})
    session.add(r)
    session.commit()

    apply_reading(r, reading_of(r))

    assert r.basket_total == 33.5
    assert r.purchase_date == date(2026, 5, 1)


def test_backfill_ignores_errored_and_unchecked_rows(session):
    from scripts.backfill_basket_totals import reading_of

    unchecked = Receipt(user_id=1, post_id="a", image_key="k")
    errored = Receipt(user_id=1, post_id="b", image_key="k",
                      check_status="error",
                      check_data=json.dumps({"error": "unreadable"}))

    assert reading_of(unchecked) == {}
    assert reading_of(errored) == {}
