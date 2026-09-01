"""Changing a password must invalidate tokens issued before it.

Without this, a stolen login token stays usable for its full 7-day life even
after the victim changes their password — the reset would be useless against
the exact attack it exists to stop.
"""
from datetime import datetime

import pytest

from app.models import User
from app.security import create_token, hash_password


@pytest.fixture()
def user(session):
    u = User(first_name="Test", last_name="Member", email="member@example.com",
             password_hash=hash_password("originalpass"), instagram_handle="tester",
             status="approved", email_verified_at=datetime.utcnow(),
             password_changed_at=datetime.utcnow())
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_signin_token_works(client, user):
    res = client.post("/auth/signin", json={"email": "member@example.com",
                                            "password": "originalpass"})
    assert res.status_code == 200
    token = res.json()["token"]
    assert client.get("/auth/me", headers=auth(token)).status_code == 200


def test_password_change_kills_the_old_token(client, user):
    stolen = client.post("/auth/signin", json={"email": "member@example.com",
                                               "password": "originalpass"}).json()["token"]
    assert client.get("/auth/me", headers=auth(stolen)).status_code == 200

    # The victim changes their password from another device.
    res = client.post("/auth/me/password", headers=auth(stolen),
                      json={"currentPassword": "originalpass",
                            "newPassword": "brandnewpass"})
    assert res.status_code == 200
    fresh = res.json()["token"]

    # The token captured before the change is now dead...
    assert client.get("/auth/me", headers=auth(stolen)).status_code == 401
    # ...but the device that made the change keeps working.
    assert client.get("/auth/me", headers=auth(fresh)).status_code == 200


def test_wrong_current_password_changes_nothing(client, user):
    token = client.post("/auth/signin", json={"email": "member@example.com",
                                              "password": "originalpass"}).json()["token"]
    res = client.post("/auth/me/password", headers=auth(token),
                      json={"currentPassword": "wrongpass", "newPassword": "brandnewpass"})
    assert res.status_code == 401
    # The session must survive a failed attempt.
    assert client.get("/auth/me", headers=auth(token)).status_code == 200


def test_token_with_a_forged_pwd_claim_is_rejected(client, user, session):
    """The claim is inside the signed JWT, so it can't be edited — but a token
    minted with a stale value must still be refused."""
    user.password_changed_at = datetime(2020, 1, 1)
    session.add(user)
    session.commit()
    stale = create_token(user)          # signed, valid, but stamped in the past

    user.password_changed_at = datetime.utcnow()
    session.add(user)
    session.commit()

    assert client.get("/auth/me", headers=auth(stale)).status_code == 401


def test_same_second_reset_still_invalidates(client, user, session):
    """Regression: the pwd claim was once a whole-second timestamp, so a reset
    in the same second as the previous change left old tokens working."""
    from datetime import timedelta

    from app.security import password_stamp
    before = password_stamp(user)
    # 1 microsecond later — the tightest possible gap.
    user.password_changed_at = user.password_changed_at + timedelta(microseconds=1)
    session.add(user)
    session.commit()
    assert password_stamp(user) != before
