"""Merchant invites, verification and password resets."""
from datetime import datetime

import pytest
from sqlmodel import select

from app.models import Campaign, Merchant, MerchantApplication
from app.security import hash_password

ADMIN = {"X-Admin-Key": "dev-admin-key"}


@pytest.fixture(autouse=True)
def admin_key(monkeypatch):
    monkeypatch.setenv("CIRQLE_ADMIN_KEY", "dev-admin-key")


@pytest.fixture()
def sent(monkeypatch):
    box = []
    import app.mailer as mailer
    monkeypatch.setattr(mailer, "send_email",
                        lambda to, subject, text, html="": box.append(
                            {"to": to, "subject": subject, "text": text}) or True)
    return box


def link_token(box, index=-1):
    return box[index]["text"].split("token=")[1].split()[0].strip().split("&")[0]


@pytest.fixture()
def application(session):
    app = MerchantApplication(brand="Nike", email="brand@example.com",
                              status="approved")
    session.add(app)
    session.commit()
    session.refresh(app)
    return app


# ── Invite flow ──────────────────────────────────────────────────────────────

def test_creating_a_merchant_emails_an_invite_and_no_password(client, application, sent):
    res = client.post("/merchant", json={"applicationId": application.id},
                      headers=ADMIN)
    assert res.status_code == 201
    body = res.json()
    assert body["inviteSent"] is True
    # Nothing for a human to copy down and pass on.
    assert "password" not in body
    assert sent[0]["to"] == "brand@example.com"
    assert "merchant-set-password.html?token=" in sent[0]["text"]


def test_merchant_cannot_sign_in_before_setting_a_password(client, application, sent):
    client.post("/merchant", json={"applicationId": application.id}, headers=ADMIN)
    res = client.post("/merchant/signin", json={"email": "brand@example.com",
                                                "password": "anything123"})
    assert res.status_code == 403
    assert "invite" in res.json()["detail"].lower()


def test_invite_sets_password_and_verifies_in_one_step(client, application, session, sent):
    client.post("/merchant", json={"applicationId": application.id}, headers=ADMIN)
    res = client.post("/merchant/set-password",
                      json={"token": link_token(sent), "newPassword": "brandpass123"})
    assert res.status_code == 200

    session.expire_all()
    m = session.exec(select(Merchant)).first()
    assert m.must_set_password is False
    assert m.email_verified_at is not None

    assert client.post("/merchant/signin",
                       json={"email": "brand@example.com",
                             "password": "brandpass123"}).status_code == 200


def test_invite_link_is_single_use(client, application, sent):
    client.post("/merchant", json={"applicationId": application.id}, headers=ADMIN)
    token = link_token(sent)
    assert client.post("/merchant/set-password",
                       json={"token": token, "newPassword": "brandpass123"}).status_code == 200
    assert client.post("/merchant/set-password",
                       json={"token": token, "newPassword": "otherpass456"}).status_code == 400


def test_invite_enforces_the_password_policy(client, application, sent):
    client.post("/merchant", json={"applicationId": application.id}, headers=ADMIN)
    assert client.post("/merchant/set-password",
                       json={"token": link_token(sent), "newPassword": "short"}).status_code == 422


def test_admin_can_resend_an_invite(client, application, session, sent):
    client.post("/merchant", json={"applicationId": application.id}, headers=ADMIN)
    first = link_token(sent)
    m = session.exec(select(Merchant)).first()

    res = client.post(f"/merchant/{m.id}/resend-invite", headers=ADMIN)
    assert res.status_code == 200
    second = link_token(sent)
    assert second != first
    # The superseded link must be dead.
    assert client.post("/merchant/set-password",
                       json={"token": first, "newPassword": "brandpass123"}).status_code == 400
    assert client.post("/merchant/set-password",
                       json={"token": second, "newPassword": "brandpass123"}).status_code == 200


def test_creating_a_merchant_still_links_the_published_deal(client, application,
                                                            session, sent):
    """Existing behaviour must survive the rewrite — stats depend on it."""
    camp = Campaign(brand="Nike", title="20% off")
    session.add(camp)
    session.commit()
    session.refresh(camp)
    application.campaign_id = camp.id
    session.add(application)
    session.commit()

    client.post("/merchant", json={"applicationId": application.id}, headers=ADMIN)
    session.expire_all()
    m = session.exec(select(Merchant)).first()
    assert session.get(Campaign, camp.id).merchant_id == m.id


# ── Forgotten password ───────────────────────────────────────────────────────

@pytest.fixture()
def live_merchant(session):
    m = Merchant(email="brand@example.com", password_hash=hash_password("brandpass123"),
                 business_name="Nike", email_verified_at=datetime.utcnow(),
                 password_changed_at=datetime.utcnow(), must_set_password=False)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def test_merchant_forgot_password_does_not_reveal_existence(client, live_merchant, sent):
    real = client.post("/merchant/forgot-password", json={"email": "brand@example.com"})
    fake = client.post("/merchant/forgot-password", json={"email": "nobody@example.com"})
    assert real.status_code == fake.status_code == 200
    assert real.json() == fake.json()
    assert len(sent) == 1


def test_merchant_reset_works_and_signs_out_devices(client, live_merchant, sent):
    old = client.post("/merchant/signin", json={"email": "brand@example.com",
                                                "password": "brandpass123"}).json()["token"]
    assert client.get("/merchant/me",
                      headers={"Authorization": f"Bearer {old}"}).status_code == 200

    client.post("/merchant/forgot-password", json={"email": "brand@example.com"})
    assert client.post("/merchant/reset-password",
                       json={"token": link_token(sent),
                             "newPassword": "newbrandpass9"}).status_code == 200

    assert client.get("/merchant/me",
                      headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert client.post("/merchant/signin", json={"email": "brand@example.com",
                                                 "password": "newbrandpass9"}).status_code == 200


def test_forgot_password_resends_the_invite_if_never_set(client, application, sent):
    """A brand that never used its invite should get the invite again, not a
    reset link for a password that doesn't exist."""
    client.post("/merchant", json={"applicationId": application.id}, headers=ADMIN)
    sent.clear()
    client.post("/merchant/forgot-password", json={"email": "brand@example.com"})
    assert "Set up your Cirqle merchant account" == sent[-1]["subject"]


def test_member_token_cannot_reach_merchant_endpoints(client, live_merchant, session):
    """The two logins must stay separate — a regression here would be severe."""
    from app.models import User
    from app.security import create_token
    u = User(first_name="A", last_name="B", email="a@b.com",
             password_hash=hash_password("whatever12"), status="approved",
             password_changed_at=datetime.utcnow())
    session.add(u)
    session.commit()
    session.refresh(u)
    assert client.get("/merchant/me",
                      headers={"Authorization": f"Bearer {create_token(u)}"}).status_code == 401
