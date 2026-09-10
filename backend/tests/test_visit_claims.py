"""Phase D — visit claims: a receipt with no Instagram post behind it.

The reason this exists: a claim used to require a post and was unique on
(user_id, post_id), so a customer who came back four times without posting was
recorded once. Every retention figure was really measuring repeat POSTING.

The dangerous case, and the one most of these tests circle, is that visit claims
all carry post_id = "". Anything that looks a claim up by (user, post_id) will
match a member's LAST visit and quietly overwrite it — collapsing their repeat
custom into a single row, which is precisely what the feature exists to prevent.
"""
import io
from datetime import date, timedelta

import pytest
from sqlmodel import Session, select

from app.models import Campaign, Merchant, Receipt, User
from app.security import create_merchant_token, create_token


def _png(seed: str) -> bytes:
    """A distinct byte string per call — the duplicate check hashes the file."""
    return b"\x89PNG\r\n\x1a\n" + seed.encode() + b"\x00" * 32


def _merchant(session: Session) -> Merchant:
    m = Merchant(email="shop@example.com", password_hash="x", business_name="Shop")
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def _campaign(session: Session, m: Merchant, *, visits=False, visit_earn=0.0,
              earn="£10.00") -> Campaign:
    c = Campaign(brand="Shop", card_title="Deal", merchant_id=m.id, earn=earn,
                 visits_enabled=visits, visit_earn=visit_earn)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _member(session: Session, n=1) -> User:
    u = User(first_name="M", last_name=str(n), email=f"m{n}@x.com",
             password_hash="x", instagram_handle=f"member{n}",
             status="approved", email_verified_at=date.today())
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_token(user)}"}


def _upload(client, user, campaign, *, post_id="", seed="a", referred=None):
    data = {"campaign_id": str(campaign.id)}
    if post_id:
        data["post_id"] = post_id
    if referred:
        data["referred_by_handle"] = referred
    return client.post("/receipts", data=data, headers=_auth(user),
                       files={"image": (f"{seed}.png", io.BytesIO(_png(seed)),
                                        "image/png")})


@pytest.fixture(autouse=True)
def _no_textract(monkeypatch):
    """The automated check calls AWS; it's out of scope here."""
    monkeypatch.setenv("CIRQLE_RECEIPT_CHECK", "off")


# ── The core reason the feature exists ───────────────────────────────────────

def test_two_visits_are_two_claims(session, client):
    """The bug this prevents: both visits carry post_id = "", so a lookup by
    (user, post_id) would treat the second as a replacement for the first and
    the member's repeat custom would vanish."""
    m = _merchant(session)
    c = _campaign(session, m, visits=True, visit_earn=2.0)
    u = _member(session)

    assert _upload(client, u, c, seed="first").status_code == 201
    assert _upload(client, u, c, seed="second").status_code == 201

    rows = session.exec(select(Receipt).where(Receipt.user_id == u.id)).all()
    assert len(rows) == 2
    assert {r.claim_kind for r in rows} == {"visit"}


def test_a_visit_claim_pays_the_visit_rate(session, client):
    m = _merchant(session)
    c = _campaign(session, m, visits=True, visit_earn=2.5, earn="£10.00")
    u = _member(session)

    res = _upload(client, u, c, seed="v")

    assert res.status_code == 201
    (row,) = session.exec(select(Receipt).where(Receipt.user_id == u.id)).all()
    assert row.claim_kind == "visit"
    assert row.amount == 2.5           # not the £10 posted rate


def test_a_post_claim_still_pays_the_posted_rate(session, client):
    m = _merchant(session)
    c = _campaign(session, m, visits=True, visit_earn=2.5, earn="£10.00")
    u = _member(session)

    _upload(client, u, c, post_id="ig-1", seed="p")

    (row,) = session.exec(select(Receipt).where(Receipt.user_id == u.id)).all()
    assert row.claim_kind == "post"
    assert row.amount == 10.0


def test_re_uploading_for_the_same_post_still_replaces(session, client):
    """The original behaviour has to survive untouched."""
    m = _merchant(session)
    c = _campaign(session, m)
    u = _member(session)

    _upload(client, u, c, post_id="ig-1", seed="one")
    _upload(client, u, c, post_id="ig-1", seed="two")

    rows = session.exec(select(Receipt).where(Receipt.user_id == u.id)).all()
    assert len(rows) == 1


# ── Opting in ────────────────────────────────────────────────────────────────

def test_visits_are_refused_unless_the_merchant_opted_in(session, client):
    """Off by default, like referral bonuses — funding a wallet must never
    enrol a deal the brand didn't choose."""
    m = _merchant(session)
    c = _campaign(session, m, visits=False)
    u = _member(session)

    res = _upload(client, u, c, seed="v")

    assert res.status_code == 422
    assert "repeat visits" in res.json()["detail"]


def test_a_visit_must_name_a_deal(session, client):
    """With no post to anchor it, nothing else says whose shop this is from."""
    m = _merchant(session)
    _campaign(session, m, visits=True)
    u = _member(session)

    res = client.post("/receipts", data={}, headers=_auth(u),
                      files={"image": ("a.png", io.BytesIO(_png("a")), "image/png")})

    assert res.status_code == 422
    assert "which deal" in res.json()["detail"]


def test_merchant_can_turn_visits_on(session, client):
    m = _merchant(session)
    c = _campaign(session, m, earn="£10.00")

    res = client.patch(f"/merchant/deals/{c.id}/visits",
                       json={"enabled": True, "earn": 3.0},
                       headers={"Authorization": f"Bearer {create_merchant_token(m)}"})

    assert res.status_code == 200
    assert res.json()["visitsEnabled"] is True
    assert res.json()["visitEarn"] == 3.0


def test_a_visit_cannot_pay_more_than_the_deal(session, client):
    """A fat-finger guard: if a repeat visit paid more than a posted claim,
    posting would be the worse option."""
    m = _merchant(session)
    c = _campaign(session, m, earn="£5.00")

    res = client.patch(f"/merchant/deals/{c.id}/visits",
                       json={"enabled": True, "earn": 50.0},
                       headers={"Authorization": f"Bearer {create_merchant_token(m)}"})

    assert res.status_code == 422


def test_visits_toggle_is_scoped_to_the_owner(session, client):
    mine = _merchant(session)
    theirs = Merchant(email="other@example.com", password_hash="x",
                      business_name="Other")
    session.add(theirs)
    session.commit()
    session.refresh(theirs)
    c = _campaign(session, theirs)

    res = client.patch(f"/merchant/deals/{c.id}/visits",
                       json={"enabled": True, "earn": 1.0},
                       headers={"Authorization": f"Bearer {create_merchant_token(mine)}"})

    assert res.status_code == 404


# ── Referrals and visits don't mix ───────────────────────────────────────────

def test_a_visit_cannot_name_a_referrer(session, client):
    """Being referred is about how you found the place — the first visit.
    Letting a repeat trip name one would pay a bonus for a customer the
    merchant already had."""
    m = _merchant(session)
    c = _campaign(session, m, visits=True, visit_earn=1.0)
    alice, bob = _member(session, 1), _member(session, 2)
    _upload(client, alice, c, post_id="ig-a", seed="alice")

    res = _upload(client, bob, c, seed="bob", referred="member1")

    assert res.status_code == 422
    assert "first claim" in res.json()["detail"]


def test_a_visit_claim_does_not_make_someone_a_valid_referrer(session, client):
    """A visit proves they shopped here, not that they posted anything anyone
    could have been referred by."""
    m = _merchant(session)
    c = _campaign(session, m, visits=True, visit_earn=1.0)
    alice, bob = _member(session, 1), _member(session, 2)
    _upload(client, alice, c, seed="alice-visit")        # visit only, no post

    res = _upload(client, bob, c, post_id="ig-b", seed="bob", referred="member1")

    assert res.status_code == 422
    assert "hasn't claimed this deal" in res.json()["detail"]


# ── Duplicate protection carries more weight now ─────────────────────────────

def test_the_same_image_cannot_back_two_visits(session, client):
    """Without a post to anchor to, the image hash is doing more of the work."""
    m = _merchant(session)
    c = _campaign(session, m, visits=True, visit_earn=1.0)
    u = _member(session)

    assert _upload(client, u, c, seed="same").status_code == 201
    res = _upload(client, u, c, seed="same")

    assert res.status_code == 409


# ── Retention now counts real visits ─────────────────────────────────────────

def test_visit_claims_make_a_customer_sticky(session):
    """The payoff. Before this, a member who posted once and came back without
    posting read as a one-timer."""
    from app.customers import build_customers

    m = _merchant(session)
    c = _campaign(session, m, visits=True, visit_earn=1.0)
    u = _member(session)
    today = date.today()
    session.add(Receipt(user_id=u.id, post_id="ig-1", claim_kind="post",
                        campaign_id=c.id, amount=10.0, image_key="k",
                        status="confirmed", basket_total=40.0,
                        purchase_date=today - timedelta(days=40)))
    session.add(Receipt(user_id=u.id, post_id="", claim_kind="visit",
                        campaign_id=c.id, amount=1.0, image_key="k2",
                        status="confirmed", basket_total=35.0,
                        purchase_date=today - timedelta(days=20)))
    session.commit()

    (rec,) = build_customers(m.id, session)

    assert rec.claims == 2
    assert rec.sticky is True
    assert rec.spend == 75.0
