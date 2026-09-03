"""Referral attribution: who may be named as a referrer, and what merchants see.

A member can say "@someone referred me" when they upload a receipt. That only
means anything if the named member claimed the SAME deal themselves, so these
tests pin down which handles are accepted and what the member is told about a
referrer whose own claim hasn't been approved yet.
"""
import io

import pytest
from sqlmodel import select

from app.models import Campaign, Merchant, Receipt, User
from app.security import create_merchant_token, create_token, hash_password


@pytest.fixture(autouse=True)
def no_storage(monkeypatch):
    """Keep uploads out of the filesystem, and off the receipt-reading service.

    Each upload gets its own digest so the duplicate-image check (which is not
    what these tests are about) never trips.
    """
    import app.routers.receipts as receipts

    counter = {"n": 0}

    def fake_upload(file):
        counter["n"] += 1
        return f"receipts/test-{counter['n']}.jpg", f"digest-{counter['n']}"

    monkeypatch.setattr(receipts, "upload_receipt", fake_upload)
    monkeypatch.setattr(receipts, "delete_receipt", lambda key: None)
    monkeypatch.setenv("CIRQLE_RECEIPT_CHECK", "off")


def make_user(session, handle, email=None):
    user = User(first_name="Test", last_name="Member",
                email=email or f"{handle}@example.com",
                password_hash=hash_password("correcthorse7"),
                instagram_handle=handle, status="approved")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def make_campaign(session, merchant_id=None, brand="Nike", earn="£10.00"):
    camp = Campaign(brand=brand, card_title=f"{brand} deal", earn=earn,
                    merchant_id=merchant_id)
    session.add(camp)
    session.commit()
    session.refresh(camp)
    return camp


def make_claim(session, user, campaign, status="pending", post_id=None):
    """A receipt this member already uploaded for a deal, in a given state."""
    receipt = Receipt(user_id=user.id, post_id=post_id or f"post-{user.id}-{campaign.id}",
                      campaign_id=campaign.id, brand=campaign.brand, amount=10.0,
                      image_key=f"receipts/seed-{user.id}-{campaign.id}.jpg",
                      status=status)
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt


def upload(client, user, campaign_id, referred_by=None, post_id="my-post"):
    data = {"post_id": post_id}
    if campaign_id is not None:
        data["campaign_id"] = str(campaign_id)
    if referred_by is not None:
        data["referred_by_handle"] = referred_by
    return client.post(
        "/receipts",
        data=data,
        files={"image": ("receipt.jpg", io.BytesIO(b"not-a-real-jpeg"), "image/jpeg")},
        headers={"Authorization": f"Bearer {create_token(user)}"},
    )


# ── Who may be named as a referrer ───────────────────────────────────────────

def test_referrer_who_never_claimed_the_deal_is_refused(client, session):
    referrer = make_user(session, "alice")
    buyer = make_user(session, "bob")
    camp = make_campaign(session)

    res = upload(client, buyer, camp.id, referred_by="@alice")

    assert res.status_code == 422
    assert "hasn't claimed this deal" in res.json()["detail"]
    assert session.exec(select(Receipt).where(Receipt.user_id == buyer.id)).first() is None


def test_referrer_who_claimed_a_different_deal_is_refused(client, session):
    referrer = make_user(session, "alice")
    buyer = make_user(session, "bob")
    theirs = make_campaign(session, brand="Adidas")
    mine = make_campaign(session, brand="Nike")
    make_claim(session, referrer, theirs, status="verified")

    res = upload(client, buyer, mine.id, referred_by="@alice")

    assert res.status_code == 422
    assert "hasn't claimed this deal" in res.json()["detail"]


def test_referrer_whose_claim_was_rejected_is_refused(client, session):
    referrer = make_user(session, "alice")
    buyer = make_user(session, "bob")
    camp = make_campaign(session)
    make_claim(session, referrer, camp, status="rejected")

    res = upload(client, buyer, camp.id, referred_by="@alice")

    assert res.status_code == 422
    assert "hasn't claimed this deal" in res.json()["detail"]


def test_referrer_with_an_unapproved_claim_is_accepted_but_flagged_pending(client, session):
    """We don't punish the referee for our own admin backlog — but we say so."""
    referrer = make_user(session, "alice")
    buyer = make_user(session, "bob")
    camp = make_campaign(session)
    make_claim(session, referrer, camp, status="pending")

    res = upload(client, buyer, camp.id, referred_by="@alice")

    assert res.status_code == 201
    body = res.json()
    assert body["referralStatus"] == "pending"
    assert body["referredByHandle"] == "@alice"

    stored = session.exec(select(Receipt).where(Receipt.user_id == buyer.id)).first()
    assert stored.referred_by_user_id == referrer.id


@pytest.mark.parametrize("status", ["verified", "confirmed", "paid"])
def test_referrer_with_an_approved_claim_is_verified(client, session, status):
    referrer = make_user(session, "alice")
    buyer = make_user(session, "bob")
    camp = make_campaign(session)
    make_claim(session, referrer, camp, status=status)

    res = upload(client, buyer, camp.id, referred_by="@alice")

    assert res.status_code == 201
    assert res.json()["referralStatus"] == "verified"


def test_handle_is_stored_as_typed(client, session):
    """The user id is the truth, but a dispute is judged on what was entered."""
    make_user(session, "alice")
    buyer = make_user(session, "bob")
    camp = make_campaign(session)
    make_claim(session, make_user(session, "alice2"), camp)  # noise
    make_claim(session, session.exec(
        select(User).where(User.instagram_handle == "alice")).first(), camp)

    res = upload(client, buyer, camp.id, referred_by="  @ALICE  ")

    assert res.status_code == 201
    stored = session.exec(select(Receipt).where(Receipt.user_id == buyer.id)).first()
    assert stored.referred_by_handle == "@ALICE"


def test_unknown_handle_is_refused(client, session):
    buyer = make_user(session, "bob")
    camp = make_campaign(session)

    res = upload(client, buyer, camp.id, referred_by="@nobody")

    assert res.status_code == 422
    assert res.json()["detail"] == "Referrer handle not found."


def test_self_referral_is_refused(client, session):
    buyer = make_user(session, "bob")
    camp = make_campaign(session)
    make_claim(session, buyer, camp, status="verified", post_id="other-post")

    res = upload(client, buyer, camp.id, referred_by="@bob")

    assert res.status_code == 422
    assert res.json()["detail"] == "You can't refer yourself."


def test_referral_without_a_deal_is_refused(client, session):
    """Without a deal there's nothing to check the referrer's claim against."""
    make_user(session, "alice")
    buyer = make_user(session, "bob")

    res = upload(client, buyer, None, referred_by="@alice")

    assert res.status_code == 422
    assert "Choose which deal" in res.json()["detail"]


def test_upload_without_a_referral_is_unaffected(client, session):
    buyer = make_user(session, "bob")
    camp = make_campaign(session)

    res = upload(client, buyer, camp.id)

    assert res.status_code == 201
    assert res.json()["referralStatus"] == ""
    assert res.json()["referredByHandle"] == ""


# ── What the merchant's Referrals panel counts ───────────────────────────────

def merchant_referrals(client, session, merchant):
    return client.get("/merchant/referrals",
                      headers={"Authorization": f"Bearer {create_merchant_token(merchant)}"})


@pytest.fixture()
def merchant(session):
    m = Merchant(business_name="Nike", email="nike@example.com",
                 password_hash=hash_password("correcthorse7"), status="active")
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def test_panel_counts_only_claims_that_earned_cashback(client, session, merchant):
    """A rejected referred claim isn't a referral the merchant benefited from."""
    referrer = make_user(session, "alice")
    camp = make_campaign(session, merchant_id=merchant.id)
    make_claim(session, referrer, camp, status="verified")

    for i, status in enumerate(["confirmed", "rejected", "pending"]):
        buyer = make_user(session, f"buyer{i}")
        claim = make_claim(session, buyer, camp, status=status)
        claim.referred_by_user_id = referrer.id
        session.add(claim)
    session.commit()

    res = merchant_referrals(client, session, merchant)

    assert res.status_code == 200
    row = res.json()[0]
    assert row["claims"] == 1                 # the confirmed one only
    assert row["referredCashback"] == 10.0    # and the two figures agree


def test_panel_hides_a_referrer_with_nothing_credited(client, session, merchant):
    referrer = make_user(session, "alice")
    buyer = make_user(session, "bob")
    camp = make_campaign(session, merchant_id=merchant.id)
    make_claim(session, referrer, camp, status="verified")
    claim = make_claim(session, buyer, camp, status="rejected")
    claim.referred_by_user_id = referrer.id
    session.add(claim)
    session.commit()

    assert merchant_referrals(client, session, merchant).json() == []


def test_panel_shows_the_referrers_earliest_post_as_the_origin(client, session, merchant):
    """With several posts on one deal, the chain starts at the first."""
    from datetime import datetime

    referrer = make_user(session, "alice")
    buyer = make_user(session, "bob")
    camp = make_campaign(session, merchant_id=merchant.id)

    first = make_claim(session, referrer, camp, status="verified", post_id="post-first")
    first.uploaded_at = datetime(2026, 1, 1)
    later = make_claim(session, referrer, camp, status="verified", post_id="post-later")
    later.uploaded_at = datetime(2026, 6, 1)
    claim = make_claim(session, buyer, camp, status="confirmed")
    claim.referred_by_user_id = referrer.id
    session.add_all([first, later, claim])
    session.commit()

    row = merchant_referrals(client, session, merchant).json()[0]

    assert row["postId"] == "post-first"
