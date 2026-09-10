"""The whole referral journey, driven through the real HTTP endpoints.

The other referral tests exercise the rules directly. This one walks the path a
real pair of members and a real brand would take — sign-up state, uploads, admin
approval, the 3-day hold, both dashboards, the merchant's bill, a withdrawal, and
finally an admin cancelling it — to prove the pieces fit together and not just
individually.
"""
import io
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.models import (Campaign, Merchant, MerchantTransaction, Receipt,
                        ReferralReward, User)
from app.security import create_merchant_token, create_token, hash_password

ADMIN = {"X-Admin-Key": "dev-admin-key"}


@pytest.fixture(autouse=True)
def no_storage(monkeypatch):
    """Keep uploads off the filesystem and off the receipt-reading service."""
    import app.routers.receipts as receipts
    n = {"i": 0}

    def fake_upload(file):
        n["i"] += 1
        return f"receipts/e2e-{n['i']}.jpg", f"digest-{n['i']}"

    monkeypatch.setattr(receipts, "upload_receipt", fake_upload)
    monkeypatch.setattr(receipts, "delete_receipt", lambda key: None)
    monkeypatch.setenv("CIRQLE_RECEIPT_CHECK", "off")


def auth(user):
    return {"Authorization": f"Bearer {create_token(user)}"}


def upload(client, user, campaign_id, post_id, referred_by=None):
    data = {"post_id": post_id, "campaign_id": str(campaign_id)}
    if referred_by:
        data["referred_by_handle"] = referred_by
    return client.post(
        "/receipts", data=data,
        files={"image": ("r.jpg", io.BytesIO(b"jpeg-bytes"), "image/jpeg")},
        headers=auth(user))


def make_member(session, handle, verified=True):
    """A member who has been through Stripe's identity checks."""
    u = User(first_name=handle.title(), last_name="Member",
             email=f"{handle}@example.com",
             password_hash=hash_password("correcthorse7"),
             instagram_handle=handle, status="approved",
             payouts_enabled=verified, payout_details_submitted=verified,
             stripe_account_id=f"acct_{handle}",
             payout_fingerprint=f"bank-{handle}" if verified else "",
             identity_fingerprint=f"id-{handle}" if verified else "")
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def age_claim(session, receipt_id, days):
    """Push a claim's date back so its 3-day hold has passed."""
    r = session.get(Receipt, receipt_id)
    r.uploaded_at = datetime.utcnow() - timedelta(days=days)
    session.add(r)
    session.commit()
    return r


@pytest.fixture()
def brand(session):
    """A brand with a live deal, both wallets funded, referrals switched on."""
    m = Merchant(business_name="Nike", email="nike@example.com",
                 password_hash=hash_password("correcthorse7"), status="active",
                 tier="growth", subscription_status="active")
    session.add(m)
    session.commit()
    session.refresh(m)

    deal = Campaign(brand="Nike", card_title="Nike trainers", earn="£10.00",
                    merchant_id=m.id, referrals_enabled=True)
    session.add(deal)
    for wallet, amount in (("cashback", 500.0), ("referral", 15.0)):
        session.add(MerchantTransaction(
            merchant_id=m.id, kind="topup", wallet=wallet, amount=amount,
            description="Card top-up", created_at=datetime(2026, 1, 1)))
    session.commit()
    session.refresh(deal)
    return m, deal


def test_the_whole_referral_journey(client, session, brand):
    merchant, deal = brand
    alice = make_member(session, "alice")     # posts about the deal
    bob = make_member(session, "bob")         # buys through her post

    # ── 1. Alice claims the deal herself ─────────────────────────────────────
    res = upload(client, alice, deal.id, "alice-post")
    assert res.status_code == 201
    alice_claim = res.json()["id"]

    # ── 2. Bob can't name her yet — nothing stops him claiming, but her own
    #       claim isn't approved, so the bonus can't be earned on it yet.
    res = upload(client, bob, deal.id, "bob-post", referred_by="@alice")
    assert res.status_code == 201
    assert res.json()["referralStatus"] == "pending"
    assert res.json()["referredByHandle"] == "@alice"
    bob_claim = res.json()["id"]

    # Nothing is payable while both claims sit in the review queue.
    stats = client.get("/account/stats", headers=auth(alice)).json()
    assert stats["referralEarnings"] == 0
    assert session.exec(select(ReferralReward)).all() == []

    # ── 3. The admin approves both claims ────────────────────────────────────
    for claim_id in (alice_claim, bob_claim):
        assert client.post(f"/receipts/{claim_id}/verify",
                           headers=ADMIN).status_code == 200

    # Still nothing: Bob's claim hasn't cleared its 3-day hold.
    client.get("/account/stats", headers=auth(alice))
    assert session.exec(select(ReferralReward)).all() == []

    # ── 4. The hold passes ───────────────────────────────────────────────────
    age_claim(session, alice_claim, days=12)
    age_claim(session, bob_claim, days=8)

    # ── 5. Alice looks at her dashboard — the bonuses are banked ─────────────
    stats = client.get("/account/stats", headers=auth(alice)).json()
    assert stats["referralEarnings"] == 1.0
    assert stats["referralCount"] == 1
    assert stats["wallet"] == 11.0                 # £10 cashback + £1 bonus

    bob_stats = client.get("/account/stats", headers=auth(bob)).json()
    assert bob_stats["referralEarnings"] == 0.5
    assert bob_stats["wallet"] == 10.5

    # ── 6. Each sees the other on their referrals list ───────────────────────
    a = client.get("/account/referrals", headers=auth(alice)).json()[0]
    b = client.get("/account/referrals", headers=auth(bob)).json()[0]
    assert (a["role"], a["handle"], a["amount"], a["status"]) == \
           ("referrer", "bob", 1.0, "earned")
    assert (b["role"], b["handle"], b["amount"], b["status"]) == \
           ("referee", "alice", 0.5, "earned")

    # ── 7. The brand has been charged £1.50, from the referral wallet only ───
    billing = client.get("/merchant/billing",
                         headers={"Authorization": f"Bearer {create_merchant_token(merchant)}"}).json()
    wallets = {w["wallet"]: w for w in billing["wallets"]}
    assert wallets["referral"]["spent"] == 1.5
    assert wallets["referral"]["balance"] == 13.5
    # The cashback wallet is untouched by the bonuses — the point of two pots.
    # It still reads the full £500 because the merchant's bill counts a claim
    # only once it is STORED as confirmed/paid, and an admin approval stores
    # "verified". That gap is pre-existing and unrelated to referrals.
    assert wallets["cashback"]["balance"] == 500.0
    assert wallets["cashback"]["toppedUp"] == 500.0

    # ── 8. The admin can see both sides ──────────────────────────────────────
    rows = {r["kind"]: r for r in client.get("/receipts/admin/referrals",
                                             headers=ADMIN).json()}
    assert rows["referrer"]["memberHandle"] == "alice"
    assert rows["referee"]["memberHandle"] == "bob"
    assert rows["referrer"]["status"] == rows["referee"]["status"] == "available"


def test_withdrawing_marks_the_bonus_paid(client, session, brand, monkeypatch):
    """Money-critical: a bonus goes out in the same transfer as the cashback,
    and must be marked paid by the same success — or it stays withdrawable
    forever and pays out again and again."""
    import app.routers.account as account

    merchant, deal = brand
    alice = make_member(session, "alice")
    bob = make_member(session, "bob")

    alice_claim = upload(client, alice, deal.id, "alice-post").json()["id"]
    bob_claim = upload(client, bob, deal.id, "bob-post",
                       referred_by="@alice").json()["id"]
    for claim_id in (alice_claim, bob_claim):
        client.post(f"/receipts/{claim_id}/verify", headers=ADMIN)
    age_claim(session, alice_claim, days=12)
    age_claim(session, bob_claim, days=8)

    # Stripe stands in: the transfer "succeeds" without leaving the test.
    monkeypatch.setattr(account, "payments_configured", lambda: True)
    monkeypatch.setattr(account, "_sync_connect", lambda user, session: None)
    sent = {}
    monkeypatch.setattr(account, "create_transfer",
                        lambda acct, amount, user_id: sent.setdefault("amount", amount) or "tr_1")

    res = client.post("/account/withdraw", headers=auth(alice))

    assert res.status_code == 200
    assert sent["amount"] == 11.0            # £10 cashback + the £1 bonus
    reward = session.exec(select(ReferralReward).where(
        ReferralReward.user_id == alice.id)).first()
    session.refresh(reward)
    assert reward.status == "paid"

    # And it can't be withdrawn a second time.
    after = res.json()
    assert after["wallet"] == 0.0
    assert after["paidOut"] == 11.0


def test_an_admin_cancel_undoes_both_sides_and_refunds_the_brand(
        client, session, brand):
    merchant, deal = brand
    alice = make_member(session, "alice")
    bob = make_member(session, "bob")

    alice_claim = upload(client, alice, deal.id, "alice-post").json()["id"]
    bob_claim = upload(client, bob, deal.id, "bob-post",
                       referred_by="@alice").json()["id"]
    for claim_id in (alice_claim, bob_claim):
        client.post(f"/receipts/{claim_id}/verify", headers=ADMIN)
    age_claim(session, alice_claim, days=12)
    age_claim(session, bob_claim, days=8)
    client.get("/account/stats", headers=auth(alice))     # banks the bonuses

    res = client.post(f"/receipts/admin/referrals/{bob_claim}/cancel", headers=ADMIN)

    assert res.status_code == 200
    assert {r["status"] for r in res.json()} == {"cancelled"}
    assert client.get("/account/stats", headers=auth(alice)).json()["wallet"] == 10.0
    assert client.get("/account/stats", headers=auth(bob)).json()["wallet"] == 10.0
    billing = client.get("/merchant/billing",
                         headers={"Authorization": f"Bearer {create_merchant_token(merchant)}"}).json()
    referral = next(w for w in billing["wallets"] if w["wallet"] == "referral")
    assert referral["balance"] == 15.0


def test_rejecting_the_buyers_claim_takes_both_bonuses_back(client, session, brand):
    merchant, deal = brand
    alice = make_member(session, "alice")
    bob = make_member(session, "bob")

    alice_claim = upload(client, alice, deal.id, "alice-post").json()["id"]
    bob_claim = upload(client, bob, deal.id, "bob-post",
                       referred_by="@alice").json()["id"]
    for claim_id in (alice_claim, bob_claim):
        client.post(f"/receipts/{claim_id}/verify", headers=ADMIN)
    age_claim(session, alice_claim, days=12)
    age_claim(session, bob_claim, days=8)
    client.get("/account/stats", headers=auth(alice))

    assert client.post(f"/receipts/{bob_claim}/reject",
                       headers=ADMIN).status_code == 200

    rewards = session.exec(select(ReferralReward)).all()
    for r in rewards:
        session.refresh(r)
    assert {r.status for r in rewards} == {"cancelled"}
    # And settling again doesn't quietly re-create them.
    client.get("/account/stats", headers=auth(alice))
    assert len(session.exec(select(ReferralReward)).all()) == 2


def test_a_deal_with_referrals_off_pays_nobody_end_to_end(client, session, brand):
    merchant, deal = brand
    deal.referrals_enabled = False
    session.add(deal)
    session.commit()

    alice = make_member(session, "alice")
    bob = make_member(session, "bob")
    alice_claim = upload(client, alice, deal.id, "alice-post").json()["id"]
    bob_claim = upload(client, bob, deal.id, "bob-post",
                       referred_by="@alice").json()["id"]
    for claim_id in (alice_claim, bob_claim):
        client.post(f"/receipts/{claim_id}/verify", headers=ADMIN)
    age_claim(session, alice_claim, days=12)
    age_claim(session, bob_claim, days=8)

    stats = client.get("/account/stats", headers=auth(alice)).json()
    assert stats["referralEarnings"] == 0
    item = client.get("/account/referrals", headers=auth(alice)).json()[0]
    assert item["status"] == "waiting"
    assert item["reason"] == "This deal doesn't offer a referral bonus."
