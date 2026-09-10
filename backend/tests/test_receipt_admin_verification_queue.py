"""The two new admin queues: GET /receipts/admin/manual-review and
GET /receipts/admin/auto-approved -- filtering and sort order."""
from app.models import Campaign, Receipt, User

ADMIN = {"X-Admin-Key": "dev-admin-key"}


def _user(session, n):
    u = User(first_name="A", last_name=str(n), email=f"a{n}@b.com", password_hash="x")
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _campaign(session):
    c = Campaign(brand="Acme", title="Acme deal", earn="£13.00")
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def test_manual_review_queue_only_shows_pending_admin_review(client, session):
    campaign = _campaign(session)
    statuses = ["pending", "verified", "auto_approved", "pending_admin_review",
               "admin_approved", "admin_rejected"]
    for i, status in enumerate(statuses):
        user = _user(session, i)
        session.add(Receipt(user_id=user.id, post_id=f"p{i}", campaign_id=campaign.id,
                            brand="Acme", amount=13.0, image_key="k",
                            verification_status=status))
    session.commit()

    resp = client.get("/receipts/admin/manual-review", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["verificationStatus"] == "pending_admin_review"


def test_manual_review_queue_includes_a_reason(client, session):
    campaign = _campaign(session)
    user = _user(session, 1)
    session.add(Receipt(user_id=user.id, post_id="p1", campaign_id=campaign.id,
                        brand="Acme", amount=13.0, image_key="k",
                        verification_status="pending_admin_review",
                        verification_error="merchant reference receipt not ready"))
    session.commit()

    resp = client.get("/receipts/admin/manual-review", headers=ADMIN)
    body = resp.json()
    assert len(body) == 1
    assert "verification failed" in body[0]["reason"]


def test_auto_approved_queue_sorted_ascending_by_score(client, session):
    campaign = _campaign(session)
    for i, score in enumerate([0.95, 0.86, 0.99]):
        user = _user(session, i)
        session.add(Receipt(user_id=user.id, post_id=f"p{i}", campaign_id=campaign.id,
                            brand="Acme", amount=13.0, image_key="k",
                            verification_status="auto_approved", overall_score=score))
    session.commit()

    resp = client.get("/receipts/admin/auto-approved", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    scores = [row["overallScore"] for row in body]
    assert scores == sorted(scores)   # ascending -- borderline cases first


def test_admin_endpoints_require_admin_key(client, session):
    resp = client.get("/receipts/admin/manual-review")
    assert resp.status_code == 401
    resp2 = client.get("/receipts/admin/auto-approved")
    assert resp2.status_code == 401
