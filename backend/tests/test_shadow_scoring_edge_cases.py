"""Edge cases for _apply_shadow_scoring that real receipts can hit but the
happy-path tests don't exercise: no linked campaign, a missing Mention row
for the claimed post, and the try/except safety net actually catching."""
import json

from app.models import Campaign, Receipt, User
from app.routers.receipts import _apply_shadow_scoring


def _seed_user(session):
    user = User(first_name="A", last_name="B", email="a@b.com", password_hash="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_receipt_with_no_campaign_still_verifies(session):
    user = _seed_user(session)
    receipt = Receipt(user_id=user.id, post_id="postNoCampaign", campaign_id=None,
                      brand="", amount=0.0, image_key="k")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    _apply_shadow_scoring(receipt, session)

    assert receipt.status == "verified"
    assert receipt.shadow_payout is None   # nothing to score against


def test_receipt_whose_mention_row_is_missing_defaults_to_zero_engagement(session):
    user = _seed_user(session)
    campaign = Campaign(brand="Acme", title="Acme deal", earn="£13.00", cashback_mode="flat")
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    # post_id references a Mention row that doesn't exist (e.g. the user
    # deleted the post and re-refreshed their feed after uploading the receipt).
    receipt = Receipt(user_id=user.id, post_id="postDeleted", campaign_id=campaign.id,
                      brand="Acme", amount=13.0, image_key="k")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    _apply_shadow_scoring(receipt, session)

    assert receipt.status == "verified"
    assert receipt.amount == 13.0   # flat payout unaffected
    assert receipt.shadow_payout is not None
    snapshot = json.loads(receipt.engagement_snapshot)
    assert snapshot["likes"] == 0 and snapshot["comments"] == 0


def test_scoring_exception_falls_back_to_verified_without_crashing(session, monkeypatch):
    user = _seed_user(session)
    campaign = Campaign(brand="Acme", title="Acme deal", earn="£13.00", cashback_mode="performance",
                        base_cashback=20.0, budget_total=100.0, budget_remaining=100.0)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    receipt = Receipt(user_id=user.id, post_id="postBoom", campaign_id=campaign.id,
                      brand="Acme", amount=13.0, image_key="k")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated scoring failure")

    monkeypatch.setattr("app.routers.receipts.compute_aqs", _boom)

    _apply_shadow_scoring(receipt, session)  # must not raise

    assert receipt.status == "verified"
    assert receipt.amount == 13.0            # untouched — real payout never applied on failure
    assert campaign.budget_remaining == 100.0  # untouched
