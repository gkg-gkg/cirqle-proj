"""Live-HTTP tests for gaps the direct-function unit tests don't cover:
- campaigns.py's new performance-mode fields through the real multipart
  create/update endpoints (budget seeding, budget-delta top-ups)
- what happens if an already-verified performance-mode receipt gets
  re-verified (bulk-verify has no status guard against this)
"""
import json
from datetime import datetime, timedelta

from app.models import Mention, Receipt, User

ADMIN = {"X-Admin-Key": "dev-admin-key"}


def _create_campaign(client, **payload_overrides):
    payload = {"brand": "Acme", "title": "Acme deal", "earn": "£13.00"}
    payload.update(payload_overrides)
    resp = client.post("/campaigns", data={"payload": json.dumps(payload)}, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_campaign_seeds_budget_remaining_from_budget_total(client):
    out = _create_campaign(client, cashbackMode="performance", baseCashback=20.0, budgetTotal=500.0)
    assert out["cashbackMode"] == "performance"
    assert out["baseCashback"] == 20.0
    assert out["budgetTotal"] == 500.0
    assert out["budgetRemaining"] == 500.0


def test_update_campaign_budget_total_adjusts_remaining_by_delta(client):
    out = _create_campaign(client, cashbackMode="performance", baseCashback=20.0, budgetTotal=500.0)
    campaign_id = out["id"]

    # Simulate some spend already happened.
    resp = client.get(f"/campaigns/{campaign_id}")
    assert resp.json()["budgetRemaining"] == 500.0

    # Now top up by +200 (not reset to 200) -> remaining should also go up by 200.
    payload = {"budgetTotal": 700.0}
    resp = client.patch(f"/campaigns/{campaign_id}", data={"payload": json.dumps(payload)},
                        headers=ADMIN)
    assert resp.status_code == 200, resp.text
    out2 = resp.json()
    assert out2["budgetTotal"] == 700.0
    assert out2["budgetRemaining"] == 700.0   # 500 + (700-500) delta = 700, since none was spent yet


def test_update_campaign_budget_delta_preserves_prior_spend(client, session):
    out = _create_campaign(client, cashbackMode="performance", baseCashback=20.0, budgetTotal=500.0)
    campaign_id = out["id"]

    # Manually simulate 300 already spent (budget_remaining=200) the way
    # _apply_shadow_scoring would after a real approval.
    from app.models import Campaign
    c = session.get(Campaign, campaign_id)
    c.budget_remaining = 200.0
    session.add(c)
    session.commit()

    # Top up total by +100 -> remaining should become 300 (200 + 100 delta), NOT reset to 600.
    payload = {"budgetTotal": 600.0}
    resp = client.patch(f"/campaigns/{campaign_id}", data={"payload": json.dumps(payload)},
                        headers=ADMIN)
    assert resp.status_code == 200, resp.text
    out2 = resp.json()
    assert out2["budgetTotal"] == 600.0
    assert out2["budgetRemaining"] == 300.0


def test_reverifying_an_already_verified_performance_receipt_does_not_double_charge_budget(client, session):
    """Regression check: bulk_verify_receipts' guard is `status not in ("pending",
    "verified")` -> skip, which means an ALREADY-verified receipt is NOT skipped
    and gets reprocessed. For a performance-mode campaign this must not deduct
    the budget a second time or silently change a member's already-paid amount.
    """
    user = User(first_name="A", last_name="B", email="a@b.com", password_hash="x")
    session.add(user)
    session.commit()
    session.refresh(user)

    out = _create_campaign(client, cashbackMode="performance", baseCashback=20.0,
                           expectedEngagementBaseline=50, budgetTotal=100.0)
    campaign_id = out["id"]

    mention = Mention(id="postX", user_id=user.id, likes_count=50, comments_count=0,
                      follower_count_at_scrape=1000, following_count_at_scrape=100,
                      timestamp=(datetime.utcnow() - timedelta(days=1)).isoformat() + "Z",
                      scraped_at=datetime.utcnow())
    session.add(mention)

    receipt = Receipt(user_id=user.id, post_id="postX", campaign_id=campaign_id,
                      brand="Acme", amount=13.0, image_key="k")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    resp1 = client.post(f"/receipts/{receipt.id}/verify", headers=ADMIN)
    assert resp1.status_code == 200, resp1.text
    first_amount = resp1.json()["amount"]

    from app.models import Campaign
    remaining_after_first = session.get(Campaign, campaign_id).budget_remaining

    # Re-verify the SAME already-verified receipt (e.g. admin double-clicks, or
    # re-runs bulk-verify over a range that includes it).
    resp2 = client.post(f"/receipts/{receipt.id}/verify", headers=ADMIN)
    assert resp2.status_code == 200, resp2.text
    second_amount = resp2.json()["amount"]

    session.refresh(receipt)
    remaining_after_second = session.get(Campaign, campaign_id).budget_remaining

    assert second_amount == first_amount, "re-verifying changed the member's paid amount"
    assert remaining_after_second == remaining_after_first, (
        "budget was deducted a second time for the same receipt")
