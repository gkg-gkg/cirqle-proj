"""verify_receipt / bulk_verify_receipts / reject_receipt: the new
verification_status/decision_source/decision_at writes and cashback-trigger
call counts. trigger_cashback_calculation is always mocked here."""
from datetime import datetime, timedelta

from app.models import Campaign, Mention, Receipt, User

ADMIN = {"X-Admin-Key": "dev-admin-key"}


def _seed(session, handle="creator"):
    user = User(first_name="A", last_name="B", email="a@b.com", password_hash="x",
               instagram_handle=handle)
    session.add(user)
    session.commit()
    session.refresh(user)

    campaign = Campaign(brand="Acme", title="Acme deal", earn="£13.00")
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    session.add(Mention(id="postA", user_id=user.id, owner_username=handle,
                        timestamp=(datetime.utcnow() - timedelta(days=1)).isoformat() + "Z"))
    session.commit()

    receipt = Receipt(user_id=user.id, post_id="postA", campaign_id=campaign.id,
                      brand="Acme", amount=13.0, image_key="k")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return user, campaign, receipt


def _spy(monkeypatch):
    calls = []
    monkeypatch.setattr("app.routers.receipts.trigger_cashback_calculation",
                        lambda receipt_id: calls.append(receipt_id))
    return calls


def test_verify_stamps_admin_decision_and_fires_cashback_trigger(client, session, monkeypatch):
    calls = _spy(monkeypatch)
    user, campaign, receipt = _seed(session)

    resp = client.post(f"/receipts/{receipt.id}/verify", headers=ADMIN)
    assert resp.status_code == 200, resp.text

    session.refresh(receipt)
    assert receipt.status == "verified"
    assert receipt.verification_status == "admin_approved"
    assert receipt.decision_source == "admin"
    assert receipt.decision_at is not None
    assert calls == [receipt.id]


def test_reject_stamps_admin_decision_without_firing_cashback(client, session, monkeypatch):
    calls = _spy(monkeypatch)
    user, campaign, receipt = _seed(session)

    resp = client.post(f"/receipts/{receipt.id}/reject", headers=ADMIN)
    assert resp.status_code == 200, resp.text

    session.refresh(receipt)
    assert receipt.status == "rejected"
    assert receipt.verification_status == "admin_rejected"
    assert receipt.decision_source == "admin"
    assert receipt.decision_at is not None
    assert calls == []


def test_bulk_verify_stamps_decision_and_fires_cashback_per_receipt(client, session, monkeypatch):
    calls = _spy(monkeypatch)
    _, campaign, r1 = _seed(session, handle="creator1")
    user2 = User(first_name="C", last_name="D", email="c@d.com", password_hash="x",
                instagram_handle="creator2")
    session.add(user2)
    session.commit()
    session.refresh(user2)
    session.add(Mention(id="postB", user_id=user2.id, owner_username="creator2",
                        timestamp=(datetime.utcnow() - timedelta(days=1)).isoformat() + "Z"))
    session.commit()
    r2 = Receipt(user_id=user2.id, post_id="postB", campaign_id=campaign.id,
                brand="Acme", amount=13.0, image_key="k2")
    session.add(r2)
    session.commit()
    session.refresh(r2)

    resp = client.post("/receipts/admin/bulk-verify", json={"ids": [r1.id, r2.id]}, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["approved"] == 2

    session.refresh(r1)
    session.refresh(r2)
    assert r1.verification_status == "admin_approved"
    assert r2.verification_status == "admin_approved"
    assert sorted(calls) == sorted([r1.id, r2.id])
