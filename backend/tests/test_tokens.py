"""The security properties of emailed link tokens must actually hold."""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.models import AuthToken
from app.tokens import _hash, issue, redeem


def test_raw_token_is_never_stored(session):
    raw = issue(session, "reset_password", "user", 1)
    stored = session.exec(select(AuthToken)).all()
    assert len(stored) == 1
    # A database leak must not hand over usable tokens.
    assert stored[0].token_hash != raw
    assert stored[0].token_hash == _hash(raw)


def test_redeem_returns_the_token_then_burns_it(session):
    raw = issue(session, "reset_password", "user", 7)
    first = redeem(session, raw, "reset_password")
    assert first is not None and first.subject_id == 7
    # Clicking the same link twice must not work.
    assert redeem(session, raw, "reset_password") is None


def test_wrong_kind_is_rejected(session):
    """A verification link must not double as a password-reset link."""
    raw = issue(session, "verify_email", "user", 1)
    assert redeem(session, raw, "reset_password") is None


def test_expired_token_is_rejected(session):
    raw = issue(session, "reset_password", "user", 1)
    token = session.exec(select(AuthToken)).first()
    token.expires_at = datetime.utcnow() - timedelta(seconds=1)
    session.add(token)
    session.commit()
    assert redeem(session, raw, "reset_password") is None


def test_issuing_again_burns_the_previous_link(session):
    """Requesting a second reset email must kill the first link."""
    old = issue(session, "reset_password", "user", 1)
    new = issue(session, "reset_password", "user", 1)
    assert redeem(session, old, "reset_password") is None
    assert redeem(session, new, "reset_password") is not None


def test_other_subjects_are_untouched(session):
    """Re-issuing for one account must not invalidate another's link."""
    theirs = issue(session, "reset_password", "user", 1)
    issue(session, "reset_password", "user", 2)
    assert redeem(session, theirs, "reset_password") is not None


def test_merchant_and_user_ids_do_not_collide(session):
    """User 5 and merchant 5 are different subjects."""
    user_raw = issue(session, "reset_password", "user", 5)
    merch_raw = issue(session, "reset_password", "merchant", 5)
    assert redeem(session, user_raw, "reset_password").subject_type == "user"
    assert redeem(session, merch_raw, "reset_password").subject_type == "merchant"


@pytest.mark.parametrize("bad", ["", "not-a-real-token", "x" * 43])
def test_garbage_is_rejected(session, bad):
    assert redeem(session, bad, "reset_password") is None


def test_tokens_are_unique(session):
    raws = {issue(session, "verify_email", "user", i) for i in range(50)}
    assert len(raws) == 50
