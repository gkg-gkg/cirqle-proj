"""Referral bonuses: £1 to the referrer, 50p to the person they referred.

Both hang off the REFERRED claim and are paid the moment it clears. Most of
these tests are about a bonus NOT being paid — the checks exist to stop one
person referring themselves from a second account, and they fail closed.
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app import referrals
from app.models import (Campaign, Merchant, MerchantTransaction, Receipt,
                        ReferralReward, User)
from app.security import create_merchant_token, create_token, hash_password

LONG_AGO = datetime(2026, 1, 1)
# routers/campaigns.py reads CIRQLE_ADMIN_KEY once at import, so the tests use
# its default rather than trying to set the variable afterwards.
ADMIN = {"X-Admin-Key": "dev-admin-key"}


# ── Fixtures and helpers ─────────────────────────────────────────────────────

@pytest.fixture()
def merchant(session):
    m = Merchant(business_name="Nike", email="nike@example.com",
                 password_hash=hash_password("correcthorse7"), status="active")
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


@pytest.fixture()
def campaign(session, merchant):
    return make_campaign(session, merchant)


def make_campaign(session, merchant, referrals_on=True, brand="Nike"):
    c = Campaign(brand=brand, card_title=f"{brand} deal", earn="£10.00",
                 merchant_id=merchant.id, referrals_enabled=referrals_on)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def make_user(session, handle, verified=True, payout_fp=None, identity_fp=None):
    """A member. `verified` means they finished Stripe's identity checks, which
    is also when the fingerprints arrive — so they default to matching that."""
    u = User(first_name="Test", last_name="Member", email=f"{handle}@example.com",
             password_hash=hash_password("correcthorse7"), instagram_handle=handle,
             status="approved", payouts_enabled=verified,
             payout_details_submitted=verified,
             payout_fingerprint=(f"bank-{handle}" if payout_fp is None and verified
                                 else (payout_fp or "")),
             identity_fingerprint=(f"id-{handle}" if identity_fp is None and verified
                                   else (identity_fp or "")))
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def make_claim(session, user, campaign, status="verified", days_ago=30,
               referred_by=None, post_id=None):
    """A cashback claim. Old enough by default that the 3-day hold has cleared,
    so a 'verified' claim reads as confirmed."""
    r = Receipt(user_id=user.id, post_id=post_id or f"post-{user.id}-{campaign.id}",
                campaign_id=campaign.id, brand=campaign.brand, amount=10.0,
                image_key=f"receipts/{user.id}-{campaign.id}.jpg", status=status,
                uploaded_at=datetime.utcnow() - timedelta(days=days_ago),
                referred_by_user_id=referred_by.id if referred_by else None)
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


def fund_referral_wallet(session, merchant, amount=15.0):
    session.add(MerchantTransaction(
        merchant_id=merchant.id, kind="topup", wallet="referral", amount=amount,
        description="Card top-up — referral wallet", created_at=LONG_AGO))
    session.commit()


def fund_cashback_wallet(session, merchant, amount=500.0):
    session.add(MerchantTransaction(
        merchant_id=merchant.id, kind="topup", wallet="cashback", amount=amount,
        description="Card top-up", created_at=LONG_AGO))
    session.commit()


@pytest.fixture()
def pair(session, campaign, merchant):
    """A referral ready to pay: A promoted the deal first, B bought after."""
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    make_claim(session, referrer, campaign, days_ago=40)
    claim = make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)
    fund_referral_wallet(session, merchant)
    return referrer, referee, claim


def rewards_for(session, user):
    return session.exec(select(ReferralReward).where(
        ReferralReward.user_id == user.id)).all()


def all_rewards(session):
    return session.exec(select(ReferralReward)).all()


# ── The happy path: both sides paid ──────────────────────────────────────────

def test_a_genuine_referral_pays_both_sides(session, pair, merchant):
    referrer, referee, claim = pair

    referrals.settle_for_user(referrer, session)

    a = rewards_for(session, referrer)[0]
    b = rewards_for(session, referee)[0]
    assert (a.kind, a.amount) == ("referrer", 1.0)
    assert (b.kind, b.amount) == ("referee", 0.5)
    assert a.receipt_id == b.receipt_id == claim.id
    assert a.merchant_id == b.merchant_id == merchant.id


def test_both_bonuses_come_out_of_the_referral_wallet(session, pair, merchant):
    referrer, _referee, _claim = pair
    assert referrals.referral_balance(merchant.id, session) == 15.0

    referrals.settle_for_user(referrer, session)

    assert referrals.referral_balance(merchant.id, session) == 13.5


def test_the_referee_can_settle_their_own_fifty_pence(session, pair):
    """Either side looking at their dashboard banks the pair."""
    _referrer, referee, _claim = pair

    referrals.settle_for_user(referee, session)

    assert len(all_rewards(session)) == 2


def test_settling_twice_does_not_pay_twice(session, pair):
    referrer, referee, _claim = pair

    referrals.settle_for_user(referrer, session)
    referrals.settle_for_user(referrer, session)
    referrals.settle_for_user(referee, session)

    assert len(all_rewards(session)) == 2


# ── Who funds it ─────────────────────────────────────────────────────────────

def test_an_empty_referral_wallet_blocks_both_bonuses(session, campaign, merchant):
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    make_claim(session, referrer, campaign, days_ago=40)
    make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)

    referrals.settle_for_user(referrer, session)

    assert all_rewards(session) == []


def test_a_wallet_holding_one_pound_funds_no_referral_at_all(
        session, campaign, merchant):
    """A referral costs £1.50 and is all-or-nothing — never half of one."""
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    make_claim(session, referrer, campaign, days_ago=40)
    make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)
    fund_referral_wallet(session, merchant, 1.0)

    referrals.settle_for_user(referrer, session)

    assert all_rewards(session) == []
    assert referrals.referral_balance(merchant.id, session) == 1.0


def test_a_full_cashback_wallet_does_not_pay_referral_bonuses(
        session, campaign, merchant):
    """The two pots are genuinely separate, not one pot shown twice."""
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    make_claim(session, referrer, campaign, days_ago=40)
    claim = make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)
    fund_cashback_wallet(session, merchant, 500.0)

    referrals.settle_for_user(referrer, session)

    assert all_rewards(session) == []
    _ok, reason = referrals.check(claim, session)
    assert "referral wallet is empty" in reason


def test_the_wallet_funds_only_as_many_referrals_as_it_holds(
        session, campaign, merchant):
    referrer = make_user(session, "alice")
    make_claim(session, referrer, campaign, days_ago=40)
    for i in range(3):
        friend = make_user(session, f"friend{i}")
        make_claim(session, friend, campaign, days_ago=20, referred_by=referrer)
    fund_referral_wallet(session, merchant, 3.0)     # two referrals' worth

    referrals.settle_for_user(referrer, session)

    assert len(all_rewards(session)) == 4            # two referrals, two sides each
    assert referrals.referral_balance(merchant.id, session) == 0.0


# ── Per-deal control ─────────────────────────────────────────────────────────

def test_a_deal_with_referrals_switched_off_pays_nothing(session, merchant):
    deal = make_campaign(session, merchant, referrals_on=False)
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    make_claim(session, referrer, deal, days_ago=40)
    claim = make_claim(session, referee, deal, days_ago=20, referred_by=referrer)
    fund_referral_wallet(session, merchant)

    referrals.settle_for_user(referrer, session)

    assert all_rewards(session) == []
    _ok, reason = referrals.check(claim, session)
    assert reason == "This deal doesn't offer a referral bonus."


def test_a_merchant_can_switch_referrals_on_for_their_deal(
        client, session, merchant):
    deal = make_campaign(session, merchant, referrals_on=False)

    res = client.patch(f"/merchant/deals/{deal.id}/referrals",
                       json={"enabled": True},
                       headers={"Authorization": f"Bearer {create_merchant_token(merchant)}"})

    assert res.status_code == 200
    assert res.json()["referralsEnabled"] is True
    session.refresh(deal)
    assert deal.referrals_enabled is True


def test_a_merchant_cannot_switch_referrals_on_someone_elses_deal(
        client, session, merchant):
    other = Merchant(business_name="Adidas", email="adidas@example.com",
                     password_hash=hash_password("correcthorse7"), status="active")
    session.add(other)
    session.commit()
    session.refresh(other)
    deal = make_campaign(session, other, referrals_on=False, brand="Adidas")

    res = client.patch(f"/merchant/deals/{deal.id}/referrals",
                       json={"enabled": True},
                       headers={"Authorization": f"Bearer {create_merchant_token(merchant)}"})

    assert res.status_code == 404


# ── The checks that stop a fake referral ─────────────────────────────────────

def test_no_bonus_while_the_referrers_own_claim_is_still_awaiting_approval(
        session, campaign, merchant):
    """The referrer is paid only once an admin has approved their own claim."""
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    own = make_claim(session, referrer, campaign, status="pending", days_ago=40)
    make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)
    fund_referral_wallet(session, merchant)

    referrals.settle_for_user(referrer, session)
    assert all_rewards(session) == []

    own.status = "verified"
    session.add(own)
    session.commit()

    referrals.settle_for_user(referrer, session)
    assert len(all_rewards(session)) == 2


def test_no_bonus_if_the_referrers_own_claim_was_rejected_after_upload(
        session, pair):
    """The upload-time answer isn't trusted — the check is run again here."""
    referrer, _referee, _claim = pair
    own = session.exec(select(Receipt).where(Receipt.user_id == referrer.id)).first()
    own.status = "rejected"
    session.add(own)
    session.commit()

    referrals.settle_for_user(referrer, session)

    assert all_rewards(session) == []


def test_no_bonus_while_the_referred_claim_is_still_clearing(
        session, campaign, merchant):
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    make_claim(session, referrer, campaign, days_ago=40)
    make_claim(session, referee, campaign, days_ago=1, referred_by=referrer)
    fund_referral_wallet(session, merchant)

    referrals.settle_for_user(referrer, session)

    assert all_rewards(session) == []


def test_no_bonus_if_the_referrer_posted_after_the_purchase(
        session, campaign, merchant):
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    make_claim(session, referrer, campaign, days_ago=10)      # posted later
    make_claim(session, referee, campaign, days_ago=30, referred_by=referrer)
    fund_referral_wallet(session, merchant)

    referrals.settle_for_user(referrer, session)

    assert all_rewards(session) == []


def test_no_bonus_if_the_referee_had_already_claimed_this_deal(
        session, campaign, merchant):
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    make_claim(session, referrer, campaign, days_ago=60)
    make_claim(session, referee, campaign, days_ago=40, post_id="bob-first")
    make_claim(session, referee, campaign, days_ago=20, referred_by=referrer,
               post_id="bob-second")
    fund_referral_wallet(session, merchant)

    referrals.settle_for_user(referrer, session)

    assert all_rewards(session) == []


# ── Two accounts, one person ─────────────────────────────────────────────────

def test_accounts_sharing_a_bank_account_cannot_refer_each_other(
        session, campaign, merchant):
    referrer = make_user(session, "alice", payout_fp="same-bank")
    referee = make_user(session, "bob", payout_fp="same-bank")
    make_claim(session, referrer, campaign, days_ago=40)
    make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)
    fund_referral_wallet(session, merchant)

    referrals.settle_for_user(referrer, session)

    assert all_rewards(session) == []


def test_accounts_sharing_a_verified_identity_cannot_refer_each_other(
        session, campaign, merchant):
    """Same person, two different bank accounts — caught on name + date of birth."""
    referrer = make_user(session, "alice", identity_fp="same-id")
    referee = make_user(session, "bob", identity_fp="same-id")
    make_claim(session, referrer, campaign, days_ago=40)
    make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)
    fund_referral_wallet(session, merchant)

    referrals.settle_for_user(referrer, session)

    assert all_rewards(session) == []


def test_missing_identity_data_blocks_the_bonus_rather_than_waving_it_through(
        session, campaign, merchant):
    """Fails closed. With nothing to compare we can't rule out one person."""
    referrer = make_user(session, "alice", payout_fp="", identity_fp="")
    referee = make_user(session, "bob")
    make_claim(session, referrer, campaign, days_ago=40)
    claim = make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)
    fund_referral_wallet(session, merchant)

    referrals.settle_for_user(referrer, session)
    assert all_rewards(session) == []
    _ok, reason = referrals.check(claim, session)
    assert "waiting on bank details" in reason

    # Stripe comes back with the details — now it pays.
    referrer.payout_fingerprint = "bank-alice"
    referrer.identity_fingerprint = "id-alice"
    session.add(referrer)
    session.commit()

    referrals.settle_for_user(referrer, session)
    assert len(all_rewards(session)) == 2


def test_the_bonus_is_held_until_both_have_verified_their_identity(
        session, campaign, merchant):
    """Held rather than refused — it pays out once they finish onboarding."""
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob", verified=False)
    make_claim(session, referrer, campaign, days_ago=40)
    make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)
    fund_referral_wallet(session, merchant)

    referrals.settle_for_user(referrer, session)
    assert all_rewards(session) == []

    referee.payouts_enabled = True
    referee.payout_fingerprint = "bank-bob"
    referee.identity_fingerprint = "id-bob"
    session.add(referee)
    session.commit()

    referrals.settle_for_user(referrer, session)
    assert len(all_rewards(session)) == 2


# ── Taking it back ───────────────────────────────────────────────────────────

def test_rejecting_the_claim_cancels_both_unspent_bonuses(session, pair):
    referrer, _referee, claim = pair
    referrals.settle_for_user(referrer, session)

    referrals.cancel_for_receipt(claim, session, "The claim was rejected.")
    session.commit()

    assert {r.status for r in all_rewards(session)} == {"cancelled"}


def test_a_bonus_already_withdrawn_is_not_clawed_back(session, pair):
    """The money has left for someone's bank — pretending otherwise would only
    make our books disagree with reality."""
    referrer, _referee, claim = pair
    referrals.settle_for_user(referrer, session)
    paid = rewards_for(session, referrer)[0]
    paid.status = "paid"
    session.add(paid)
    session.commit()

    referrals.cancel_for_receipt(claim, session, "The claim was rejected.")
    session.commit()

    assert {r.status for r in all_rewards(session)} == {"paid", "cancelled"}


def test_cancelling_returns_the_full_cost_to_the_merchants_wallet(
        session, pair, merchant):
    referrer, _referee, claim = pair
    referrals.settle_for_user(referrer, session)
    assert referrals.referral_balance(merchant.id, session) == 13.5

    referrals.cancel_for_receipt(claim, session, "The claim was rejected.")
    session.commit()

    assert referrals.referral_balance(merchant.id, session) == 15.0


# ── Admin oversight ──────────────────────────────────────────────────────────

def test_admin_sees_both_sides_of_every_referral(client, session, pair):
    referrer, _referee, _claim = pair
    referrals.settle_for_user(referrer, session)

    res = client.get("/receipts/admin/referrals", headers=ADMIN)

    assert res.status_code == 200
    rows = {r["kind"]: r for r in res.json()}
    assert rows["referrer"]["memberHandle"] == "alice"
    assert rows["referrer"]["otherHandle"] == "bob"
    assert rows["referrer"]["amount"] == 1.0
    assert rows["referee"]["memberHandle"] == "bob"
    assert rows["referee"]["amount"] == 0.5


def test_admin_sees_why_an_unpaid_referral_has_not_paid(client, session,
                                                        campaign, merchant):
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    make_claim(session, referrer, campaign, days_ago=40)
    make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)
    # No referral wallet funding.

    res = client.get("/receipts/admin/referrals", headers=ADMIN)

    row = res.json()[0]
    assert row["status"] == "waiting"
    assert "referral wallet is empty" in row["reason"]


def test_admin_can_cancel_a_referral_and_the_money_goes_back(
        client, session, pair, merchant):
    referrer, _referee, claim = pair
    referrals.settle_for_user(referrer, session)

    res = client.post(f"/receipts/admin/referrals/{claim.id}/cancel", headers=ADMIN)

    assert res.status_code == 200
    assert {r["status"] for r in res.json()} == {"cancelled"}
    assert referrals.referral_balance(merchant.id, session) == 15.0


# ── What the members see ─────────────────────────────────────────────────────

def test_the_referrers_pound_shows_in_their_wallet(client, session, pair):
    referrer, _referee, _claim = pair

    res = client.get("/account/stats",
                     headers={"Authorization": f"Bearer {create_token(referrer)}"})

    body = res.json()
    assert body["referralEarnings"] == 1.0
    assert body["referralCount"] == 1
    assert body["wallet"] == 11.0        # £10 of their own cashback + the £1


def test_the_referees_fifty_pence_shows_in_their_wallet(client, session, pair):
    _referrer, referee, _claim = pair

    res = client.get("/account/stats",
                     headers={"Authorization": f"Bearer {create_token(referee)}"})

    body = res.json()
    assert body["referralEarnings"] == 0.5
    assert body["wallet"] == 10.5


def test_each_side_sees_their_own_role_and_the_other_person(client, session, pair):
    referrer, referee, _claim = pair
    auth = lambda u: {"Authorization": f"Bearer {create_token(u)}"}

    a = client.get("/account/referrals", headers=auth(referrer)).json()[0]
    b = client.get("/account/referrals", headers=auth(referee)).json()[0]

    assert (a["role"], a["handle"], a["amount"]) == ("referrer", "bob", 1.0)
    assert (b["role"], b["handle"], b["amount"]) == ("referee", "alice", 0.5)
    assert a["status"] == b["status"] == "earned"


def test_an_unpaid_referral_says_what_it_is_waiting_for(client, session,
                                                        campaign, merchant):
    referrer = make_user(session, "alice")
    referee = make_user(session, "bob")
    make_claim(session, referrer, campaign, days_ago=40)
    make_claim(session, referee, campaign, days_ago=20, referred_by=referrer)
    # No referral wallet funding.

    res = client.get("/account/referrals",
                     headers={"Authorization": f"Bearer {create_token(referrer)}"})

    item = res.json()[0]
    assert item["status"] == "waiting"
    assert "referral wallet is empty" in item["reason"]
    assert item["amount"] == 0


# ── What the merchant sees ───────────────────────────────────────────────────

def test_billing_reports_the_two_wallets_separately(client, session, pair, merchant):
    referrer, _referee, _claim = pair
    fund_cashback_wallet(session, merchant, 500.0)
    referrals.settle_for_user(referrer, session)
    # The merchant's bill counts claims by their STORED status, not the one that
    # clears on the clock — so bank them the way an admin approval would.
    for r in session.exec(select(Receipt)).all():
        r.status = "confirmed"
        session.add(r)
    session.commit()

    res = client.get("/merchant/billing",
                     headers={"Authorization": f"Bearer {create_merchant_token(merchant)}"})

    wallets = {w["wallet"]: w for w in res.json()["wallets"]}
    assert wallets["cashback"]["toppedUp"] == 500.0
    assert wallets["cashback"]["spent"] == 20.0
    assert wallets["cashback"]["balance"] == 480.0
    assert wallets["referral"]["toppedUp"] == 15.0
    assert wallets["referral"]["spent"] == 1.5
    assert wallets["referral"]["balance"] == 13.5


def test_deal_stats_show_referral_state_and_spend(client, session, pair, merchant):
    referrer, _referee, _claim = pair
    referrals.settle_for_user(referrer, session)

    res = client.get("/merchant/stats",
                     headers={"Authorization": f"Bearer {create_merchant_token(merchant)}"})

    deal = res.json()["deals"][0]
    assert deal["referralsEnabled"] is True
    assert deal["referralsPaid"] == 1.5


def test_topping_up_an_unknown_wallet_is_refused(client, session, merchant):
    merchant.tier = "growth"
    merchant.subscription_status = "active"
    session.add(merchant)
    session.commit()

    res = client.get("/merchant/billing/quote?amount=50&wallet=savings",
                     headers={"Authorization": f"Bearer {create_merchant_token(merchant)}"})

    assert res.status_code == 422
    assert res.json()["detail"] == "Unknown wallet."
