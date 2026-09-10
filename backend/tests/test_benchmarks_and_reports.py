"""Phase E — benchmarks, the incrementality estimate, CSV export, monthly report.

The risk in this phase is overclaiming: presenting a peer median computed from
two brands as if it meant something, or an estimate as if it were a lift study.
Most of these tests are about what the code REFUSES to say.
"""
from datetime import date, timedelta

from sqlmodel import Session

from app.benchmarks import compare
from app.customers import (INDUSTRY, MIN_CATEGORY_MERCHANTS, build_customers,
                           incrementality, new_customers)
from app.models import Campaign, Merchant, Receipt, User
from app.security import create_merchant_token

TODAY = date(2026, 9, 8)


def _merchant(session: Session, n=1, categories='["restaurant"]') -> Merchant:
    m = Merchant(email=f"shop{n}@example.com", password_hash="x",
                 business_name=f"Shop {n}", categories=categories,
                 email_verified_at=TODAY)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def _campaign(session: Session, m: Merchant) -> Campaign:
    c = Campaign(brand=m.business_name, card_title="Deal", merchant_id=m.id)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _user(session: Session, n: int) -> User:
    u = User(first_name="M", last_name=str(n), email=f"m{n}@x.com",
             password_hash="x", instagram_handle=f"member{n}", status="approved")
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _buy(session, user, c, day, *, total=20.0, amount=2.0, tag=""):
    session.add(Receipt(user_id=user.id, post_id=f"p{user.id}-{day}-{tag}",
                        campaign_id=c.id, brand=c.brand, amount=amount,
                        image_key="k", status="confirmed", basket_total=total,
                        purchase_date=day))
    session.commit()


# ── Benchmarks ───────────────────────────────────────────────────────────────

def test_falls_back_to_industry_without_enough_peers(session):
    """Two brands in a category is not a category. The published figure is a
    weaker claim than a peer median, but it's an honest one."""
    m = _merchant(session, 1)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=100))

    result = compare(m, session, TODAY)

    assert result["peerCount"] == 0
    assert result["peerCategory"] == ""
    repeat = next(x for x in result["measures"] if x["measure"] == "repeatRate")
    assert repeat["source"] == "industry"
    assert repeat["comparedTo"] == INDUSTRY["repeatRate"]["baseline"]


def test_uses_cirqle_peers_once_the_category_is_big_enough(session):
    merchants = [_merchant(session, n) for n in range(MIN_CATEGORY_MERCHANTS)]
    for i, m in enumerate(merchants):
        c = _campaign(session, m)
        u = _user(session, 100 + i)
        _buy(session, u, c, TODAY - timedelta(days=200), tag="a")
        _buy(session, u, c, TODAY - timedelta(days=150), tag="b")

    result = compare(merchants[0], session, TODAY)

    assert result["peerCategory"] == "restaurant"
    assert result["peerCount"] == MIN_CATEGORY_MERCHANTS - 1
    assert all(x["source"] == "cirqle" for x in result["measures"])


def test_merchants_in_other_categories_are_not_peers(session):
    mine = _merchant(session, 1, categories='["restaurant"]')
    c = _campaign(session, mine)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=100))
    for n in range(2, 8):
        other = _merchant(session, n, categories='["fashion"]')
        oc = _campaign(session, other)
        _buy(session, _user(session, 100 + n), oc, TODAY - timedelta(days=100))

    result = compare(mine, session, TODAY)

    assert result["peerCount"] == 0        # none of them share a category


def test_merchants_with_no_customers_are_left_out_of_the_median(session):
    """A brand that hasn't launched entering the median as zeroes would drag
    every comparison down as Cirqle signs up new partners."""
    merchants = [_merchant(session, n) for n in range(MIN_CATEGORY_MERCHANTS + 3)]
    for i, m in enumerate(merchants[:MIN_CATEGORY_MERCHANTS]):
        c = _campaign(session, m)
        u = _user(session, 200 + i)
        _buy(session, u, c, TODAY - timedelta(days=200), tag="a")
        _buy(session, u, c, TODAY - timedelta(days=150), tag="b")
    for m in merchants[MIN_CATEGORY_MERCHANTS:]:
        _campaign(session, m)              # signed up, never launched

    result = compare(merchants[0], session, TODAY)
    repeat = next(x for x in result["measures"] if x["measure"] == "repeatRate")

    assert repeat["comparedTo"] == 100.0   # the launched ones all repeat
    assert repeat["ahead"] is True


def test_merchant_with_no_customers_gets_nothing_to_compare(session):
    m = _merchant(session, 1)
    _campaign(session, m)

    assert compare(m, session, TODAY) == {"measures": [], "peerCategory": "",
                                          "peerCount": 0}


# ── Incrementality estimate ──────────────────────────────────────────────────

def test_new_customers_are_those_who_first_bought_recently(session):
    m = _merchant(session, 1)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=10))    # new
    _buy(session, _user(session, 2), c, TODAY - timedelta(days=200))   # not

    fresh = new_customers(build_customers(m.id, session), TODAY)

    assert [f.handle for f in fresh] == ["member1"]


def test_incrementality_scales_revenue_by_the_new_share(session):
    m = _merchant(session, 1)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=10), total=100.0)
    _buy(session, _user(session, 2), c, TODAY - timedelta(days=300), total=100.0)

    result = incrementality(build_customers(m.id, session), 200.0, 20.0, TODAY)

    assert result["newShare"] == 50.0
    assert result["incrementalRevenue"] == 100.0
    assert result["costPerIncrementalPound"] == 0.2
    assert result["costPerNewCustomer"] == 20.0


def test_incrementality_does_not_divide_by_zero(session):
    m = _merchant(session, 1)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=300))

    result = incrementality(build_customers(m.id, session), 0.0, 0.0, TODAY)

    assert result["newShare"] == 0.0
    assert result["costPerIncrementalPound"] == 0.0
    assert result["costPerNewCustomer"] == 0.0


def test_incrementality_endpoint_states_its_own_weakness(session, client):
    """It's an estimate, not a lift study, and the response has to say so —
    otherwise a merchant quotes it as measured incrementality in a meeting."""
    m = _merchant(session, 1)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=10))

    res = client.get("/merchant/incrementality", headers={
        "Authorization": f"Bearer {create_merchant_token(m)}"})

    assert res.status_code == 200
    assert "not a lift study" in res.json()["basis"]


# ── CSV export ───────────────────────────────────────────────────────────────

def test_csv_export_matches_the_screen_and_leaks_nothing_more(session, client):
    m = _merchant(session, 1)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=100), total=42.5)

    res = client.get("/merchant/customers.csv", headers={
        "Authorization": f"Bearer {create_merchant_token(m)}"})

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "attachment" in res.headers["content-disposition"]
    assert "member1" in res.text
    assert "42.50" in res.text
    assert "m1@x.com" not in res.text          # handle only, same as the portal


def test_csv_export_is_scoped_to_the_caller(session, client):
    mine, theirs = _merchant(session, 1), _merchant(session, 2)
    _campaign(session, mine)
    _buy(session, _user(session, 9), _campaign(session, theirs),
         TODAY - timedelta(days=30))

    res = client.get("/merchant/customers.csv", headers={
        "Authorization": f"Bearer {create_merchant_token(mine)}"})

    assert "member9" not in res.text


# ── Monthly report ───────────────────────────────────────────────────────────

def test_report_is_skipped_for_a_merchant_with_no_activity(session):
    """A page of zeroes reads as a dead product rather than a quiet month."""
    from scripts.send_monthly_reports import build_report

    m = _merchant(session, 1)
    _campaign(session, m)

    assert build_report(m, session, TODAY) is None


def test_report_headline_leads_with_the_return(session):
    from scripts.send_monthly_reports import build_report

    m = _merchant(session, 1)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=30),
         total=200.0, amount=20.0)

    headline, rows = build_report(m, session, TODAY)

    assert "10.00×" in headline
    labels = [label for label, _, _ in rows]
    assert labels[0] == "Tracked sales"
    # The coverage caveat travels with the number, since an email has no tooltip.
    assert "read from" in rows[0][2]


def test_report_says_so_when_no_totals_were_legible(session):
    from scripts.send_monthly_reports import build_report

    m = _merchant(session, 1)
    c = _campaign(session, m)
    session.add(Receipt(user_id=_user(session, 1).id, post_id="p", campaign_id=c.id,
                        brand=c.brand, amount=2.0, image_key="k",
                        status="confirmed", basket_total=None,
                        purchase_date=TODAY - timedelta(days=30)))
    session.commit()

    headline, _ = build_report(m, session, TODAY)

    assert "couldn't read a total" in headline


def test_report_period_is_the_month_just_gone(session):
    from scripts.send_monthly_reports import _period

    assert _period(date(2026, 9, 8)) == "August 2026"
    assert _period(date(2026, 1, 3)) == "December 2025"     # year rolls back
