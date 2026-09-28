"""Members without Instagram pick a Cirqle username instead.

The username shares the instagram_handle column, so the same uniqueness rules
apply — but it must never be matched to real Instagram posts."""
from sqlmodel import select

from app.models import User
from app.routers.feed import _user_handle
from tests.test_member_email_flows import SIGNUP, sent  # noqa: F401  (fixture)


def test_signup_without_instagram_is_flagged(client, session, sent):
    res = client.post("/auth/signup", json={**SIGNUP, "instagramHandle": "ada.shops", "noInstagram": True})
    assert res.status_code == 201
    user = session.exec(select(User)).first()
    assert user.instagram_handle == "ada.shops"
    assert user.has_instagram is False
    # never matched to Instagram posts
    assert _user_handle(user) == ""


def test_signup_with_instagram_still_matches_posts(client, session, sent):
    client.post("/auth/signup", json=SIGNUP)
    user = session.exec(select(User)).first()
    assert user.has_instagram is True
    assert _user_handle(user) == "ada"


def test_username_must_be_unique_against_instagram_handles(client, sent):
    client.post("/auth/signup", json=SIGNUP)  # takes "ada"
    res = client.post("/auth/signup", json={**SIGNUP, "email": "b@example.com",
                                            "instagramHandle": "ADA", "noInstagram": True})
    assert res.status_code == 409
    assert "already taken" in res.json()["detail"]
