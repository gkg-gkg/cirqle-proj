"""Signup verification, forgotten passwords, and changing an email address."""
from datetime import datetime

import pytest
from sqlmodel import select

from app.models import AuthToken, User
from app.security import hash_password
from app.tokens import _hash

SIGNUP = {"firstName": "Ada", "lastName": "Lovelace",
          "email": "ada@example.com", "password": "correcthorse7",
          "instagramHandle": "ada"}


@pytest.fixture()
def sent(monkeypatch):
    """Capture the links we'd have emailed, instead of sending anything."""
    box = []
    import app.mailer as mailer
    monkeypatch.setattr(mailer, "send_email",
                        lambda to, subject, text, html="": box.append(
                            {"to": to, "subject": subject, "text": text}) or True)
    return box


def link_token(sent_box, index=-1):
    """Pull the token out of the last link we 'sent'.

    Split on & too — the email-change link carries a &change=1 flag after the
    token, and including it would silently produce an invalid token.
    """
    text = sent_box[index]["text"]
    return text.split("token=")[1].split()[0].strip().split("&")[0]


# ── Signup + verification ────────────────────────────────────────────────────

def test_signup_lands_unverified_and_emails_a_link(client, session, sent):
    res = client.post("/auth/signup", json=SIGNUP)
    assert res.status_code == 201
    assert res.json()["status"] == "unverified"

    user = session.exec(select(User)).first()
    assert user.status == "unverified"
    assert user.email_verified_at is None
    assert len(sent) == 1
    assert sent[0]["to"] == "ada@example.com"
    assert "verify-email.html?token=" in sent[0]["text"]


def test_unverified_account_cannot_sign_in(client, sent):
    client.post("/auth/signup", json=SIGNUP)
    res = client.post("/auth/signin", json={"email": SIGNUP["email"],
                                            "password": SIGNUP["password"]})
    assert res.status_code == 403
    assert "confirm your email" in res.json()["detail"].lower()


def test_verifying_moves_the_account_into_the_approval_queue(client, session, sent):
    client.post("/auth/signup", json=SIGNUP)
    res = client.post("/auth/verify-email", json={"token": link_token(sent)})
    assert res.status_code == 200

    session.expire_all()
    user = session.exec(select(User)).first()
    assert user.email_verified_at is not None
    assert user.status == "pending"      # now the admin's problem, not before


def test_verified_but_unapproved_still_cannot_sign_in(client, sent):
    """Both gates must hold, not just the first."""
    client.post("/auth/signup", json=SIGNUP)
    client.post("/auth/verify-email", json={"token": link_token(sent)})
    res = client.post("/auth/signin", json={"email": SIGNUP["email"],
                                            "password": SIGNUP["password"]})
    assert res.status_code == 403
    assert "approval" in res.json()["detail"].lower()


def test_verification_link_cannot_be_reused(client, sent):
    client.post("/auth/signup", json=SIGNUP)
    token = link_token(sent)
    assert client.post("/auth/verify-email", json={"token": token}).status_code == 200
    assert client.post("/auth/verify-email", json={"token": token}).status_code == 400


def test_old_link_cannot_drag_an_approved_account_backwards(client, session, sent):
    """An admin-approved account must not be knocked back to 'pending' by
    someone clicking a stale verification link."""
    client.post("/auth/signup", json=SIGNUP)
    token = link_token(sent)
    user = session.exec(select(User)).first()
    user.status = "approved"
    user.email_verified_at = datetime.utcnow()
    session.add(user)
    session.commit()

    client.post("/auth/verify-email", json={"token": token})
    session.expire_all()
    assert session.exec(select(User)).first().status == "approved"


def test_resend_does_not_reveal_whether_an_account_exists(client, sent):
    client.post("/auth/signup", json=SIGNUP)
    sent.clear()
    real = client.post("/auth/resend-verification", json={"email": SIGNUP["email"]})
    fake = client.post("/auth/resend-verification", json={"email": "nobody@example.com"})
    assert real.status_code == fake.status_code == 200
    assert real.json() == fake.json()
    assert len(sent) == 1                # only the real one actually sent


# ── Forgotten password ───────────────────────────────────────────────────────

@pytest.fixture()
def member(session):
    u = User(first_name="Ada", last_name="L", email="ada@example.com",
             password_hash=hash_password("correcthorse7"), instagram_handle="ada",
             status="approved", email_verified_at=datetime.utcnow(),
             password_changed_at=datetime.utcnow())
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_forgot_password_does_not_reveal_whether_an_account_exists(client, member, sent):
    real = client.post("/auth/forgot-password", json={"email": "ada@example.com"})
    fake = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert real.status_code == fake.status_code == 200
    assert real.json() == fake.json()
    assert len(sent) == 1


def test_reset_password_works_end_to_end(client, member, sent):
    client.post("/auth/forgot-password", json={"email": "ada@example.com"})
    res = client.post("/auth/reset-password",
                      json={"token": link_token(sent), "newPassword": "brandnewpass9"})
    assert res.status_code == 200

    assert client.post("/auth/signin", json={"email": "ada@example.com",
                                             "password": "brandnewpass9"}).status_code == 200
    assert client.post("/auth/signin", json={"email": "ada@example.com",
                                             "password": "correcthorse7"}).status_code == 401


def test_reset_signs_out_every_existing_device(client, member, sent):
    old = client.post("/auth/signin", json={"email": "ada@example.com",
                                            "password": "correcthorse7"}).json()["token"]
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {old}"}).status_code == 200

    client.post("/auth/forgot-password", json={"email": "ada@example.com"})
    client.post("/auth/reset-password",
                json={"token": link_token(sent), "newPassword": "brandnewpass9"})

    assert client.get("/auth/me",
                      headers={"Authorization": f"Bearer {old}"}).status_code == 401


def test_reset_link_is_single_use(client, member, sent):
    client.post("/auth/forgot-password", json={"email": "ada@example.com"})
    token = link_token(sent)
    assert client.post("/auth/reset-password",
                       json={"token": token, "newPassword": "brandnewpass9"}).status_code == 200
    assert client.post("/auth/reset-password",
                       json={"token": token, "newPassword": "another0ne99"}).status_code == 400


def test_reset_enforces_the_password_policy(client, member, sent):
    client.post("/auth/forgot-password", json={"email": "ada@example.com"})
    res = client.post("/auth/reset-password",
                      json={"token": link_token(sent), "newPassword": "short"})
    assert res.status_code == 422


def test_rejected_accounts_get_no_reset_link(client, member, session, sent):
    member.status = "rejected"
    session.add(member)
    session.commit()
    res = client.post("/auth/forgot-password", json={"email": "ada@example.com"})
    assert res.status_code == 200          # same answer as always...
    assert len(sent) == 0                  # ...but nothing was sent


# ── Changing an email address ────────────────────────────────────────────────

def test_email_change_is_not_applied_until_confirmed(client, member, sent):
    token = client.post("/auth/signin", json={"email": "ada@example.com",
                                              "password": "correcthorse7"}).json()["token"]
    res = client.patch("/auth/me", headers={"Authorization": f"Bearer {token}"},
                       json={"email": "attacker@example.com"})
    assert res.status_code == 200
    # Still the original address — a stolen token can't move the account.
    assert res.json()["email"] == "ada@example.com"
    assert res.json()["pendingEmail"] == "attacker@example.com"
    assert client.post("/auth/signin", json={"email": "ada@example.com",
                                             "password": "correcthorse7"}).status_code == 200


def test_confirming_applies_the_new_address(client, member, sent):
    token = client.post("/auth/signin", json={"email": "ada@example.com",
                                              "password": "correcthorse7"}).json()["token"]
    client.patch("/auth/me", headers={"Authorization": f"Bearer {token}"},
                 json={"email": "new@example.com"})
    # The link goes to the NEW address — that's what proves ownership.
    assert sent[-1]["to"] == "new@example.com"

    res = client.post("/auth/confirm-email-change", json={"token": link_token(sent)})
    assert res.status_code == 200
    assert client.post("/auth/signin", json={"email": "new@example.com",
                                             "password": "correcthorse7"}).status_code == 200


def test_email_change_to_a_taken_address_is_refused(client, member, session, sent):
    session.add(User(first_name="B", last_name="C", email="taken@example.com",
                     password_hash=hash_password("whatever12"), status="approved",
                     password_changed_at=datetime.utcnow()))
    session.commit()
    token = client.post("/auth/signin", json={"email": "ada@example.com",
                                              "password": "correcthorse7"}).json()["token"]
    res = client.patch("/auth/me", headers={"Authorization": f"Bearer {token}"},
                       json={"email": "taken@example.com"})
    assert res.status_code == 409


# ── Password policy ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad,reason", [
    ("short1", "too short"),
    ("password123", "too common"),
    ("ada", "is the email local part"),
])
def test_signup_rejects_weak_passwords(client, bad, reason, sent):
    res = client.post("/auth/signup", json={**SIGNUP, "password": bad})
    assert res.status_code == 422, reason


def test_signup_still_rejects_duplicate_emails(client, member, sent):
    assert client.post("/auth/signup", json=SIGNUP).status_code == 409
