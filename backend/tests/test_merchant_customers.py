"""Phase B — customers, and whether they come back.

The bar these have to clear: a merchant looking at a healthy programme must not
be shown a declining one. Almost every test here is about maturation — refusing
to score a customer, or a cohort, over a window that has not finished yet.
"""
from datetime import date, timedelta

from sqlmodel import Session

from app.customers import (MIN_COHORT, build_customers, cohorts, lapsed,
                           median_days_to_second, repeat_rate, return_rate,
                           split_by_path, summary, third_claim_rate)
from app.models import Campaign, Merchant, Receipt, User

TODAY = date(2026, 9, 8)


def _merchant(session: Session) -> Merchant:
    m = Merchant(email="shop@example.com", password_hash="x", business_name="Shop")
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def _campaign(session: Session, m: Merchant) -> Campaign:
    c = Campaign(brand="Shop", card_title="Deal", merchant_id=m.id)
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


def _buy(session: Session, user: User, c: Campaign, day: date, *,
         total=20.0, referred_by: User = None, amount=2.0):
    r = Receipt(user_id=user.id, post_id=f"p{user.id}-{day}", campaign_id=c.id,
                brand=c.brand, amount=amount, image_key="k", status="confirmed",
                basket_total=total, purchase_date=day)
    if referred_by is not None:
        r.referred_by_user_id = referred_by.id
        r.referred_by_handle = referred_by.instagram_handle
    session.add(r)
    session.commit()
    return r


def _records(session, merchant):
    return build_customers(merchant.id, session)


# ── Building the picture ─────────────────────────────────────────────────────

def test_customer_history_is_assembled_from_their_claims(session):
    m, c = _merchant(session), None
    c = _campaign(session, m)
    u = _user(session, 1)
    _buy(session, u, c, TODAY - timedelta(days=60), total=30.0, amount=3.0)
    _buy(session, u, c, TODAY - timedelta(days=20), total=50.0, amount=3.0)

    (rec,) = _records(session, m)

    assert rec.claims == 2
    assert rec.spend == 80.0
    assert rec.cashback == 6.0
    assert rec.first == TODAY - timedelta(days=60)
    assert rec.last == TODAY - timedelta(days=20)
    assert rec.acquisition == "direct"


def test_only_credited_claims_count(session):
    m = _merchant(session)
    c = _campaign(session, m)
    u = _user(session, 1)
    _buy(session, u, c, TODAY - timedelta(days=10))
    pending = Receipt(user_id=u.id, post_id="pending", campaign_id=c.id,
                      amount=2.0, image_key="k", status="pending",
                      basket_total=99.0, purchase_date=TODAY)
    session.add(pending)
    session.commit()

    (rec,) = _records(session, m)
    assert rec.claims == 1
    assert rec.spend == 20.0


def test_referrer_is_recorded_and_counted_both_ways(session):
    m = _merchant(session)
    c = _campaign(session, m)
    alice, bob = _user(session, 1), _user(session, 2)
    _buy(session, alice, c, TODAY - timedelta(days=40))
    _buy(session, bob, c, TODAY - timedelta(days=30), referred_by=alice)

    by_id = {r.user_id: r for r in _records(session, m)}

    assert by_id[bob.id].acquisition == "referred"
    assert by_id[bob.id].referred_by_handle == "member1"
    assert by_id[alice.id].referred_count == 1
    assert by_id[alice.id].acquisition == "direct"


def test_a_customer_is_acquired_once(session):
    """A later claim naming a different member must not re-acquire someone the
    merchant already had."""
    m = _merchant(session)
    c = _campaign(session, m)
    alice, bob, carol = (_user(session, 1), _user(session, 2), _user(session, 3))
    _buy(session, alice, c, TODAY - timedelta(days=90))
    _buy(session, bob, c, TODAY - timedelta(days=80))
    _buy(session, carol, c, TODAY - timedelta(days=70), referred_by=alice)
    _buy(session, carol, c, TODAY - timedelta(days=10), referred_by=bob)

    by_id = {r.user_id: r for r in _records(session, m)}
    assert by_id[carol.id].referred_by_handle == "member1"
    assert by_id[alice.id].referred_count == 1
    assert by_id[bob.id].referred_count == 0


# ── Stickiness ───────────────────────────────────────────────────────────────

def test_second_claim_inside_seven_days_is_not_sticky(session):
    """Two receipts from one weekend is one trip, not loyalty."""
    m = _merchant(session)
    c = _campaign(session, m)
    u = _user(session, 1)
    _buy(session, u, c, TODAY - timedelta(days=40))
    _buy(session, u, c, TODAY - timedelta(days=37))          # 3 days later

    (rec,) = _records(session, m)
    assert rec.claims == 2
    assert rec.sticky is False


def test_second_claim_after_seven_days_is_sticky(session):
    m = _merchant(session)
    c = _campaign(session, m)
    u = _user(session, 1)
    _buy(session, u, c, TODAY - timedelta(days=40))
    _buy(session, u, c, TODAY - timedelta(days=30))

    (rec,) = _records(session, m)
    assert rec.sticky is True


def test_brand_new_customers_do_not_drag_the_repeat_rate_down(session):
    """The maturation rule. Someone who bought yesterday has not failed to
    return — they have not had the chance, and counting them as a failure makes
    every new customer look like churn."""
    m = _merchant(session)
    c = _campaign(session, m)
    old = _user(session, 1)
    _buy(session, old, c, TODAY - timedelta(days=60))
    _buy(session, old, c, TODAY - timedelta(days=40))
    for n in range(2, 6):                       # four customers, all from today
        _buy(session, _user(session, n), c, TODAY)

    records = _records(session, m)
    assert len(records) == 5
    assert repeat_rate(records, TODAY) == 100.0      # judged on the one matured


def test_return_rate_only_judges_matured_customers(session):
    m = _merchant(session)
    c = _campaign(session, m)
    settled = _user(session, 1)
    _buy(session, settled, c, TODAY - timedelta(days=100))
    _buy(session, settled, c, TODAY - timedelta(days=80))       # back within 30
    recent = _user(session, 2)
    _buy(session, recent, c, TODAY - timedelta(days=5))         # too new to judge

    records = _records(session, m)
    assert return_rate(records, 30, TODAY) == 100.0
    assert return_rate(records, 90, TODAY) == 100.0


def test_third_claim_rate_is_measured_against_repeaters(session):
    m = _merchant(session)
    c = _campaign(session, m)
    thrice = _user(session, 1)
    for d in (90, 60, 30):
        _buy(session, thrice, c, TODAY - timedelta(days=d))
    twice = _user(session, 2)
    for d in (90, 60):
        _buy(session, twice, c, TODAY - timedelta(days=d))
    _buy(session, _user(session, 3), c, TODAY - timedelta(days=90))   # once only

    records = _records(session, m)
    assert third_claim_rate(records) == 50.0       # 1 of the 2 repeaters


def test_median_days_to_second(session):
    m = _merchant(session)
    c = _campaign(session, m)
    for n, gap in ((1, 10), (2, 20), (3, 30)):
        u = _user(session, n)
        _buy(session, u, c, TODAY - timedelta(days=200))
        _buy(session, u, c, TODAY - timedelta(days=200 - gap))

    assert median_days_to_second(_records(session, m)) == 20


def test_median_days_to_second_is_none_without_repeaters(session):
    m = _merchant(session)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=30))

    assert median_days_to_second(_records(session, m)) is None


# ── Referred vs found-it-themselves ──────────────────────────────────────────

def test_split_by_path_separates_the_two_groups(session):
    m = _merchant(session)
    c = _campaign(session, m)
    alice = _user(session, 1)
    _buy(session, alice, c, TODAY - timedelta(days=200))

    # Five referred customers, all of whom came back.
    for n in range(10, 15):
        u = _user(session, n)
        _buy(session, u, c, TODAY - timedelta(days=180), referred_by=alice)
        _buy(session, u, c, TODAY - timedelta(days=150))
    # Five who found it themselves and never returned.
    for n in range(20, 25):
        _buy(session, _user(session, n), c, TODAY - timedelta(days=180))

    paths = split_by_path(_records(session, m), TODAY)

    assert paths["referred"]["customers"] == 5
    assert paths["referred"]["enough"] is True
    assert paths["referred"]["repeatRate"] == 100.0
    assert paths["direct"]["repeatRate"] == 0.0     # alice + the five one-timers


def test_small_groups_are_flagged_as_too_few_to_report(session):
    m = _merchant(session)
    c = _campaign(session, m)
    alice = _user(session, 1)
    _buy(session, alice, c, TODAY - timedelta(days=100))
    _buy(session, _user(session, 2), c, TODAY - timedelta(days=90),
         referred_by=alice)

    paths = split_by_path(_records(session, m), TODAY)
    assert paths["referred"]["customers"] == 1
    assert paths["referred"]["enough"] is False


# ── Cohorts ──────────────────────────────────────────────────────────────────

def test_immature_cohort_cells_are_marked_not_zero(session):
    """The bug this prevents: an unfinished month reported as a 0% return rate,
    which makes a growing merchant's dashboard look like a collapsing one."""
    m = _merchant(session)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=5))

    (row,) = cohorts(_records(session, m), TODAY)

    assert row["size"] == 1
    assert all(cell["matured"] is False for cell in row["cells"])
    assert all(cell["rate"] == 0.0 for cell in row["cells"])


def test_matured_cohort_cell_reports_a_real_rate(session):
    m = _merchant(session)
    c = _campaign(session, m)
    came_back = _user(session, 1)
    _buy(session, came_back, c, date(2026, 4, 10))
    _buy(session, came_back, c, date(2026, 5, 5))          # inside 30 days
    _buy(session, _user(session, 2), c, date(2026, 4, 12))  # never returned

    (row,) = cohorts(_records(session, m), TODAY)

    first = row["cells"][0]
    assert row["cohort"] == "2026-04"
    assert first["matured"] is True
    assert first["rate"] == 50.0


def test_cohort_below_threshold_is_flagged(session):
    m = _merchant(session)
    c = _campaign(session, m)
    for n in range(MIN_COHORT - 1):
        _buy(session, _user(session, n), c, date(2026, 4, 10))

    (row,) = cohorts(_records(session, m), TODAY)
    assert row["enough"] is False


# ── Lapse risk ───────────────────────────────────────────────────────────────

def test_a_regular_who_goes_quiet_is_lapsed(session):
    m = _merchant(session)
    c = _campaign(session, m)
    regular = _user(session, 1)
    for d in (200, 190, 180):                # every ~10 days, then nothing
        _buy(session, regular, c, TODAY - timedelta(days=d))

    assert [r.user_id for r in lapsed(_records(session, m), TODAY)] == [regular.id]


def test_an_occasional_customer_on_schedule_is_not_lapsed(session):
    """A twice-a-year customer who last came four months ago is behaving
    normally. Judging them against a weekly regular's rhythm would be wrong."""
    m = _merchant(session)
    c = _campaign(session, m)
    occasional = _user(session, 1)
    _buy(session, occasional, c, TODAY - timedelta(days=300))
    _buy(session, occasional, c, TODAY - timedelta(days=120))

    assert lapsed(_records(session, m), TODAY) == []


# ── The endpoint ─────────────────────────────────────────────────────────────

def test_summary_shape(session):
    m = _merchant(session)
    c = _campaign(session, m)
    u = _user(session, 1)
    _buy(session, u, c, TODAY - timedelta(days=60), total=40.0)
    _buy(session, u, c, TODAY - timedelta(days=30), total=60.0)

    s = summary(_records(session, m), session, m.id, TODAY)

    assert s["customers"] == 1
    assert s["sticky"] == 1
    assert s["claimsPerCustomer"] == 2.0
    assert s["revenuePerCustomer"] == 100.0
    assert s["medianDaysToSecond"] == 30


def test_merchant_with_no_customers(session):
    m = _merchant(session)
    _campaign(session, m)

    records = _records(session, m)
    assert records == []
    assert summary(records, session, m.id, TODAY)["customers"] == 0
    assert cohorts(records, TODAY) == []


def test_customers_endpoint_scopes_to_the_callers_own_deals(session, client):
    """A merchant must never see another merchant's customers."""
    from app.security import create_merchant_token

    mine, theirs = _merchant(session), Merchant(
        email="other@example.com", password_hash="x", business_name="Other")
    session.add(theirs)
    session.commit()
    session.refresh(theirs)

    _buy(session, _user(session, 1), _campaign(session, mine),
         TODAY - timedelta(days=30))
    _buy(session, _user(session, 2), _campaign(session, theirs),
         TODAY - timedelta(days=30))

    res = client.get("/merchant/customers", headers={
        "Authorization": f"Bearer {create_merchant_token(mine)}"})

    assert res.status_code == 200
    body = res.json()
    assert body["summary"]["customers"] == 1
    assert [c["handle"] for c in body["customers"]] == ["member1"]
    assert body["caveat"]


def test_customers_endpoint_returns_handle_not_email(session, client):
    """Merchants get the Instagram handle only — never the member's email."""
    from app.security import create_merchant_token

    m = _merchant(session)
    _buy(session, _user(session, 1), _campaign(session, m),
         TODAY - timedelta(days=30))

    res = client.get("/merchant/customers", headers={
        "Authorization": f"Bearer {create_merchant_token(m)}"})

    assert "m1@x.com" not in res.text
    assert res.json()["customers"][0]["handle"] == "member1"
