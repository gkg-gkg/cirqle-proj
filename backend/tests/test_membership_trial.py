"""The free first month, and the one thing that must never break about it:
a brand gets exactly one.

Stripe grants a trial to whoever the Checkout session asks for one, every
time — so cancelling and resubscribing would be a free month a month if the
only gate were Stripe's. The gate is `Merchant.trial_used`, and these tests
pin down both halves of it: that it is spent when the session is created, and
that what the portal advertises always matches what the checkout will do.
"""
import pytest
from sqlmodel import Session

from app.models import Merchant
from app.payments import TRIAL_DAYS
from app.routers.merchant import _trial_available


def _merchant(session: Session, **kw) -> Merchant:
    m = Merchant(email="shop@example.com", password_hash="x",
                 business_name="Test Shop", **kw)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def test_fresh_merchant_is_offered_the_trial(session):
    assert _trial_available(_merchant(session)) is True


def test_trial_is_not_offered_twice(session):
    assert _trial_available(_merchant(session, trial_used=True)) is False


def test_cancelling_does_not_restore_the_trial(session):
    """The whole point of the flag: status goes back to canceled and the tier
    is cleared, but the free month stays spent."""
    m = _merchant(session, trial_used=True, subscription_status="canceled", tier="")
    assert _trial_available(m) is False


def test_subscription_session_only_asks_stripe_for_a_trial_when_eligible(monkeypatch):
    """create_subscription_session must not send trial_period_days unless told
    to — this is the call that actually costs money if it is wrong."""
    from app import payments

    captured = {}

    class _FakeSession:
        url = "https://checkout.test/x"

    def _create(**kwargs):
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(payments.stripe.checkout.Session, "create", staticmethod(_create))
    monkeypatch.setattr(payments, "_secret_key", lambda: "sk_test_x")
    monkeypatch.setattr(payments, "_price_id", lambda key: "price_x")

    class _M:
        id = 1

    payments.create_subscription_session(_M(), "starter", "cus_x", "", with_trial=False)
    assert "trial_period_days" not in captured["subscription_data"]

    captured.clear()
    payments.create_subscription_session(_M(), "starter", "cus_x", "", with_trial=True)
    assert captured["subscription_data"]["trial_period_days"] == TRIAL_DAYS
    # The metadata the webhook reads to find the merchant again must survive
    # having the trial added alongside it.
    assert captured["subscription_data"]["metadata"]["tier"] == "starter"


def test_webhook_marks_a_trialing_subscription_as_active_but_flags_the_trial(session):
    """subscription_status stays the single answer to "can they use it", and
    `trialing` carries the distinction the portal needs for its wording."""
    from app.routers import stripe_webhook

    m = _merchant(session, stripe_customer_id="cus_x")
    stripe_webhook._sync_subscription(
        {"id": "sub_x", "status": "trialing", "customer": "cus_x",
         "metadata": {"merchant_id": str(m.id), "tier": "starter"},
         "current_period_end": None},
        session,
    )
    session.refresh(m)
    assert m.subscription_status == "active"   # trialing brands can trade
    assert m.trialing is True
    assert m.trial_used is True                # and cannot have a second one
