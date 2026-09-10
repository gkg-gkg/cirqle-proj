"""Smoke test: can a real, approved user actually upload a receipt for a
claimed deal through the live HTTP endpoint (the full stack: auth, storage,
duplicate detection, the background automated check, and the response
shape) -- not just internal function calls."""
import io
from datetime import datetime

from sqlmodel import select

from app.models import Campaign, Mention, Receipt, User
from app.security import create_token


def _approved_user(session, handle="creator1", email="creator1@example.com"):
    user = User(first_name="Jane", last_name="Doe", email=email,
               password_hash="x", instagram_handle=handle,
               status="approved", email_verified_at=datetime.utcnow())
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _fake_jpeg_bytes() -> bytes:
    # Minimal valid-enough bytes; storage.py only checks content_type + non-empty,
    # it doesn't actually decode the image.
    return b"\xff\xd8\xff\xe0" + b"0" * 200 + b"\xff\xd9"


def test_user_can_upload_a_receipt_for_a_claimed_deal(client, session):
    user = _approved_user(session)
    token = create_token(user)

    campaign = Campaign(brand="Acme", title="Acme deal", earn="£13.00", cashback_mode="flat")
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    session.add(Mention(id="postUpload1", user_id=user.id, owner_username="creator1",
                        likes_count=10, comments_count=2))
    session.commit()

    resp = client.post(
        "/receipts",
        headers={"Authorization": f"Bearer {token}"},
        data={"post_id": "postUpload1", "campaign_id": str(campaign.id)},
        files={"image": ("receipt.jpg", io.BytesIO(_fake_jpeg_bytes()), "image/jpeg")},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["brand"] == "Acme"
    assert body["amount"] == 13.0
    assert body["status"] in ("pending", "confirmed")

    # Confirm it actually landed in the database, not just a well-formed response.
    rows = session.exec(select(Receipt).where(Receipt.user_id == user.id)).all()
    assert len(rows) == 1
    assert rows[0].image_key   # storage actually wrote something and returned a key
    assert rows[0].status == "pending"


def test_uploading_the_same_receipt_image_twice_is_rejected(client, session):
    user = _approved_user(session, handle="creator2", email="creator2@example.com")
    token = create_token(user)

    campaign = Campaign(brand="Acme", title="Acme deal", earn="£13.00", cashback_mode="flat")
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    session.add(Mention(id="postUploadA", user_id=user.id, owner_username="creator2"))
    session.add(Mention(id="postUploadB", user_id=user.id, owner_username="creator2"))
    session.commit()

    image_bytes = _fake_jpeg_bytes()

    resp1 = client.post(
        "/receipts", headers={"Authorization": f"Bearer {token}"},
        data={"post_id": "postUploadA", "campaign_id": str(campaign.id)},
        files={"image": ("r1.jpg", io.BytesIO(image_bytes), "image/jpeg")},
    )
    assert resp1.status_code == 201, resp1.text

    # Same bytes, different post -> should be rejected as a duplicate claim.
    resp2 = client.post(
        "/receipts", headers={"Authorization": f"Bearer {token}"},
        data={"post_id": "postUploadB", "campaign_id": str(campaign.id)},
        files={"image": ("r2.jpg", io.BytesIO(image_bytes), "image/jpeg")},
    )
    assert resp2.status_code == 409, resp2.text


def test_upload_requires_authentication(client, session):
    resp = client.post(
        "/receipts",
        data={"post_id": "postX"},
        files={"image": ("r.jpg", io.BytesIO(_fake_jpeg_bytes()), "image/jpeg")},
    )
    assert resp.status_code in (401, 403)


def test_unapproved_user_cannot_upload(client, session):
    user = User(first_name="New", last_name="Guy", email="new@example.com",
               password_hash="x", instagram_handle="newguy",
               status="pending", email_verified_at=datetime.utcnow())
    session.add(user)
    session.commit()
    session.refresh(user)
    token = create_token(user)

    resp = client.post(
        "/receipts", headers={"Authorization": f"Bearer {token}"},
        data={"post_id": "postX"},
        files={"image": ("r.jpg", io.BytesIO(_fake_jpeg_bytes()), "image/jpeg")},
    )
    assert resp.status_code == 403
