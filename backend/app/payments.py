"""Stripe integration for merchant prepaid top-ups.

Merchants pre-fund a balance that pays for shopper cashback. This module wraps
the two Stripe touch-points:

  • create_topup_session() — makes a hosted Stripe Checkout page for one top-up.
    The merchant is redirected there, pays on Stripe's own domain (no card data
    ever reaches us), then bounced back to the billing tab.
  • webhook_secret()       — the signing secret the webhook route uses to prove
    an incoming callback genuinely came from Stripe.

Config is read live from the environment on each call (not cached at import), so
tests can set the keys before exercising the code:
  • STRIPE_SECRET_KEY      — the API key (sk_test_… locally, sk_live_… in prod).
  • STRIPE_WEBHOOK_SECRET  — verifies webhook callbacks (whsec_…).

The module is named `payments` (not `stripe`) so `import stripe` doesn't import
this file instead of the SDK.
"""
import os
import re

import stripe

# Mirrors main.py's CORS allowlist: we only ever build a success/cancel redirect
# back to an origin we already trust, so a spoofed Origin header can't turn this
# into an open redirect.
_DEFAULT_ORIGINS = [
    "https://gkg-gkg.github.io",
    "https://cirqle.co.uk",
    "https://www.cirqle.co.uk",
]
_DEFAULT_SITE = "https://cirqle.co.uk"
_LOCALHOST = re.compile(r"http://(localhost|127\.0\.0\.1)(:\d+)?$")


class PaymentError(RuntimeError):
    """A Stripe call failed — maps to a 502 for the caller."""


def _secret_key() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "")


def webhook_secret() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def payments_configured() -> bool:
    """True once a Stripe secret key is set (so checkout can be created)."""
    return bool(_secret_key())


def _return_base(origin: str) -> str:
    """The site origin to redirect the merchant back to after checkout.

    Uses the request's Origin header when it's one we trust (so both the live
    site and localhost dev work automatically), else falls back to production.
    """
    allowed = ([o.strip() for o in os.environ["CIRQLE_CORS_ORIGINS"].split(",") if o.strip()]
               if os.environ.get("CIRQLE_CORS_ORIGINS") else _DEFAULT_ORIGINS)
    if origin and (origin in allowed or _LOCALHOST.match(origin)):
        return origin
    return _DEFAULT_SITE


def create_topup_session(merchant, amount: float, origin: str) -> str:
    """Create a hosted Stripe Checkout Session for a £`amount` top-up.

    Returns the URL to redirect the merchant to. The balance is NOT credited
    here — that happens when Stripe calls the webhook after a successful
    payment (a redirect alone can be faked, a signed webhook can't).
    """
    stripe.api_key = _secret_key()
    base = _return_base(origin)
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "product_data": {
                        "name": "Cirqle cashback credit",
                        "description": f"Prepaid balance for {merchant.business_name or 'your account'}",
                    },
                    "unit_amount": int(round(amount * 100)),   # Stripe wants pence
                },
                "quantity": 1,
            }],
            success_url=f"{base}/merchant.html?topup=success#billing",
            cancel_url=f"{base}/merchant.html?topup=cancel#billing",
            client_reference_id=str(merchant.id),
            metadata={"merchant_id": str(merchant.id), "kind": "topup"},
        )
    except Exception as exc:  # noqa: BLE001 — surface any Stripe failure as one type
        raise PaymentError(str(exc)) from exc
    return session.url
