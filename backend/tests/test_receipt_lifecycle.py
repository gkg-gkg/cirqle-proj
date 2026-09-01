"""Integration test: mention -> receipt -> admin approval, asserting the
shadow-mode audit fields persist while real payout behavior for a flat
campaign is unchanged (spec cashback-algorithm v1.0 §10)."""
import json
from datetime import datetime, timedelta

from sqlmodel import select

from app.models import Campaign, Mention, Receipt, User


def _seed(session, handle="creator"):
    user = User(first_name="Jane", last_name="Doe", email="jane@example.com",
                password_hash="x", instagram_handle=handle)
    session.add(user)
    session.commit()
    session.refresh(user)

    campaign = Campaign(brand="Acme", title="Acme deal", earn="£13.00", cashback_mode="flat")
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    # >=3 tagged posts so consistency_score has a real (non-neutral-only) sample,
    # most recent one is what ratio/engagement-rate scoring will key off of.
    for i in range(3):
        session.add(Mention(
            id=f"post{i}", user_id=user.id, owner_username=handle,
            likes_count=40 + i, comments_count=5,
            follower_count_at_scrape=2000, following_count_at_scrape=300,
            timestamp=(datetime.utcnow() - timedelta(days=1)).isoformat() + "Z",
            scraped_at=datetime.utcnow() - timedelta(days=2 - i),
        ))
    session.commit()

    receipt = Receipt(user_id=user.id, post_id="post2", campaign_id=campaign.id,
                      brand="Acme", amount=13.0, image_key="k")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return user, campaign, receipt


def test_verify_receipt_preserves_flat_payout_and_stamps_audit_fields(client, session):
    user, campaign, receipt = _seed(session)

    resp = client.post(f"/receipts/{receipt.id}/verify", headers={"X-Admin-Key": "dev-admin-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("pending", "confirmed")  # effective_status view, not raw
    assert body["amount"] == 13.0   # real payout unchanged — shadow mode is inert

    session.refresh(receipt)
    assert receipt.status == "verified"
    assert receipt.amount == 13.0
    assert receipt.aqs_score_at_approval is not None
    assert 0.5 <= receipt.aqs_score_at_approval <= 1.0
    assert receipt.engagement_multiplier_at_approval is not None
    assert receipt.algorithm_version == "v1.0"
    assert receipt.shadow_payout is not None
    snapshot = json.loads(receipt.engagement_snapshot)
    assert "likes" in snapshot and "comments" in snapshot


def test_verify_rejects_after_window_closes(client, session):
    user, campaign, receipt = _seed(session)
    # Push the tagged post's timestamp outside the 3-day window.
    mention = session.exec(select(Mention).where(Mention.id == "post2")).first()
    mention.timestamp = (datetime.utcnow() - timedelta(days=10)).isoformat() + "Z"
    session.add(mention)
    session.commit()

    resp = client.post(f"/receipts/{receipt.id}/verify", headers={"X-Admin-Key": "dev-admin-key"})
    assert resp.status_code == 400
