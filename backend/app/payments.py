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
from datetime import datetime

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


# ─────────────────────────────────────────────────────────────────────────────
# MEMBERSHIP TIERS — the single source of truth for pricing.
#
# To change a price or allowance: edit this dict, then run
#   python scripts/setup_stripe_plans.py
# which creates/updates the matching Product + Price in Stripe. Nothing else in
# the codebase hardcodes a price. `lookup_key` is how we find the Stripe Price
# again without storing its id anywhere.
# ─────────────────────────────────────────────────────────────────────────────
TIERS = {
    "starter": {
        "name": "Starter",
        "fee": 39.0,          # £/month
        "allowance": 150.0,   # £ of fee-free top-ups per calendar month
        "lookup_key": "cirqle_starter_monthly",
        "blurb": "For brands testing cashback with a first campaign.",
    },
    "growth": {
        "name": "Growth",
        "fee": 79.0,
        "allowance": 500.0,
        "lookup_key": "cirqle_growth_monthly",
        "blurb": "For brands running cashback as a steady channel.",
    },
    "scale": {
        "name": "Scale",
        "fee": 199.0,
        "allowance": 2000.0,
        "lookup_key": "cirqle_scale_monthly",
        "blurb": "For high-volume brands with always-on campaigns.",
    },
}

# Charged on top of any top-up beyond that month's included allowance.
OVERAGE_RATE = 0.10

CURRENCY = "gbp"


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


def month_start(now=None):
    """First moment of the current calendar month — when the allowance resets."""
    now = now or datetime.utcnow()
    return datetime(now.year, now.month, 1)


def quote_topup(merchant, credit: float, used_this_month: float) -> dict:
    """Work out what a £`credit` top-up costs.

    Each calendar month the first `allowance` pounds of credit are fee-free;
    anything beyond that carries OVERAGE_RATE, added ON TOP (ask for £50 of
    credit past the allowance -> pay £55, £50 lands in the balance).
    """
    tier = TIERS.get(merchant.tier or "")
    allowance = tier["allowance"] if tier else 0.0
    left = max(0.0, allowance - used_this_month)
    charged_part = max(0.0, credit - left)          # the part past the allowance
    fee = round(charged_part * OVERAGE_RATE, 2)
    return {
        "credit": round(credit, 2),
        "fee": fee,
        "total": round(credit + fee, 2),
        "feeRate": OVERAGE_RATE if charged_part > 0 else 0.0,
        "allowanceLeft": round(left, 2),
    }


def create_topup_session(merchant, quote: dict, customer_id: str, origin: str) -> str:
    """Hosted Checkout for a prepaid top-up, with the platform fee shown as its
    own line so the merchant sees exactly what they're paying for.

    The balance is NOT credited here — that happens when Stripe calls the
    webhook after a successful payment (a redirect alone can be faked).
    """
    stripe.api_key = _secret_key()
    base = _return_base(origin)

    def line(name: str, desc: str, pounds: float) -> dict:
        return {"price_data": {"currency": CURRENCY,
                               "product_data": {"name": name, "description": desc},
                               "unit_amount": int(round(pounds * 100))},
                "quantity": 1}

    items = [line("Cirqle cashback credit",
                  f"Prepaid balance for {merchant.business_name or 'your account'}",
                  quote["credit"])]
    if quote["fee"] > 0:
        items.append(line(
            "Platform fee",
            f"{int(OVERAGE_RATE * 100)}% on credit beyond this month's included allowance",
            quote["fee"]))
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            customer=customer_id,
            line_items=items,
            success_url=f"{base}/merchant.html?topup=success#billing",
            cancel_url=f"{base}/merchant.html?topup=cancel#billing",
            client_reference_id=str(merchant.id),
            metadata={"merchant_id": str(merchant.id), "kind": "topup",
                      "credit": f"{quote['credit']:.2f}", "fee": f"{quote['fee']:.2f}"},
        )
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(str(exc)) from exc
    return session.url


# ─────────────────────────────────────────────────────────────────────────────
# Customers — the anchor every payment, saved card and invoice hangs off.
# ─────────────────────────────────────────────────────────────────────────────
def ensure_customer(merchant, session) -> str:
    """Return this merchant's Stripe customer id, creating it on first use.

    Persists the id back onto the Merchant row so we only ever make one
    customer per brand (a duplicate would split their cards and invoices).
    """
    stripe.api_key = _secret_key()
    if merchant.stripe_customer_id:
        # Confirm it still resolves. A stored id goes stale when the customer is
        # deleted in Stripe — or, critically, when the keys switch from test to
        # live, since test customers don't exist in the live account. Recreate
        # rather than failing every checkout from then on.
        try:
            existing = stripe.Customer.retrieve(merchant.stripe_customer_id).to_dict()
            if not existing.get("deleted"):
                return merchant.stripe_customer_id
        except Exception:  # noqa: BLE001 — unknown/foreign id: fall through and recreate
            pass
    try:
        customer = stripe.Customer.create(
            email=merchant.email,
            name=merchant.business_name or merchant.email,
            metadata={"merchant_id": str(merchant.id)},
        )
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(str(exc)) from exc
    merchant.stripe_customer_id = customer.id
    session.add(merchant)
    session.commit()
    session.refresh(merchant)
    return merchant.stripe_customer_id


def _price_id(tier_key: str) -> str:
    """Find the Stripe Price for a tier by its lookup key.

    Looked up rather than stored, so rotating a price in Stripe doesn't need a
    code change or a migration.
    """
    tier = TIERS[tier_key]
    stripe.api_key = _secret_key()
    prices = stripe.Price.list(lookup_keys=[tier["lookup_key"]], active=True, limit=1)
    data = prices.to_dict().get("data", [])
    if not data:
        raise PaymentError(
            f"No Stripe price for '{tier['lookup_key']}'. "
            "Run scripts/setup_stripe_plans.py to create the plans."
        )
    return data[0]["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Subscriptions — plan signup collects the card and starts billing in one step.
# ─────────────────────────────────────────────────────────────────────────────
def create_subscription_session(merchant, tier_key: str, customer_id: str,
                                origin: str) -> str:
    """Hosted Checkout that takes the card + billing address and starts the plan."""
    if tier_key not in TIERS:
        raise PaymentError(f"Unknown plan '{tier_key}'.")
    stripe.api_key = _secret_key()
    base = _return_base(origin)
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": _price_id(tier_key), "quantity": 1}],
            billing_address_collection="required",
            success_url=f"{base}/merchant.html?plan=success#billing",
            cancel_url=f"{base}/merchant.html?plan=cancel#billing",
            client_reference_id=str(merchant.id),
            metadata={"merchant_id": str(merchant.id), "kind": "subscription",
                      "tier": tier_key},
            subscription_data={"metadata": {"merchant_id": str(merchant.id),
                                            "tier": tier_key}},
        )
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(str(exc)) from exc
    return session.url


def create_portal_session(customer_id: str, origin: str) -> str:
    """Stripe's hosted Billing Portal: update card, billing address, invoices,
    change or cancel the plan. All of it is Stripe's UI, so no card data ever
    reaches us and we don't rebuild invoice history ourselves."""
    stripe.api_key = _secret_key()
    base = _return_base(origin)
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{base}/merchant.html#billing",
        )
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(str(exc)) from exc
    return session.url


def refund_topup(checkout_session_id: str, amount: float) -> str:
    """Refund £`amount` of a top-up back to the card that paid it.

    This is how a merchant gets unused balance out: no bank details and no
    Stripe Connect needed, because it reverses their own original charge.
    We stored the Checkout Session id, so resolve it to the payment first.
    """
    stripe.api_key = _secret_key()
    try:
        cs = stripe.checkout.Session.retrieve(checkout_session_id).to_dict()
        payment_intent = cs.get("payment_intent")
        if not payment_intent:
            raise PaymentError("That top-up has no charge to refund.")
        refund = stripe.Refund.create(
            payment_intent=payment_intent,
            amount=int(round(amount * 100)),
        )
    except PaymentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(str(exc)) from exc
    return refund.id


# ─────────────────────────────────────────────────────────────────────────────
# Connect — paying members their cashback.
#
# Sending money to a member is not the same as charging a card: Stripe must
# verify who they are first (identity + bank details), which is a legal
# requirement, not a Stripe preference. Each member therefore gets their own
# Express account. Stripe hosts the whole form and holds the details — we never
# see or store a bank account number, only whether they're cleared to be paid.
# ─────────────────────────────────────────────────────────────────────────────
MIN_PAYOUT = 5.0        # £ — below this, transfer fees make a payout wasteful


def ensure_connect_account(user, session) -> str:
    """Return this member's Connect account id, creating it on first use.

    Like ensure_customer, this re-checks a stored id and recreates it if it no
    longer resolves, so a test->live key swap can't strand anyone.
    """
    stripe.api_key = _secret_key()
    if user.stripe_account_id:
        try:
            acct = stripe.Account.retrieve(user.stripe_account_id).to_dict()
            if not acct.get("deleted"):
                return user.stripe_account_id
        except Exception:  # noqa: BLE001 — unknown id: fall through and recreate
            pass
    try:
        acct = stripe.Account.create(
            type="express",
            country="GB",
            email=user.email,
            capabilities={"transfers": {"requested": True}},
            business_type="individual",
            metadata={"user_id": str(user.id)},
        )
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(str(exc)) from exc
    user.stripe_account_id = acct.id
    session.add(user)
    session.commit()
    session.refresh(user)
    return user.stripe_account_id


def create_onboarding_link(account_id: str, origin: str) -> str:
    """A single-use link to Stripe's hosted identity + bank details form."""
    stripe.api_key = _secret_key()
    base = _return_base(origin)
    try:
        link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=f"{base}/dashboard.html?payout=refresh",
            return_url=f"{base}/dashboard.html?payout=done",
            type="account_onboarding",
        )
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(str(exc)) from exc
    return link.url


def connect_status(account_id: str) -> dict:
    """Ask Stripe whether this member is cleared to receive money."""
    stripe.api_key = _secret_key()
    try:
        acct = stripe.Account.retrieve(account_id).to_dict()
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(str(exc)) from exc
    return {
        "payouts_enabled": bool(acct.get("payouts_enabled")),
        "details_submitted": bool(acct.get("details_submitted")),
    }


def create_transfer(account_id: str, amount: float, user_id: int) -> str:
    """Move £`amount` from the platform balance to a member's Connect account.

    The platform balance is what merchants' top-ups have built up, so this is
    where merchant money actually becomes member money.
    """
    stripe.api_key = _secret_key()
    try:
        transfer = stripe.Transfer.create(
            amount=int(round(amount * 100)),
            currency=CURRENCY,
            destination=account_id,
            description="Cirqle cashback withdrawal",
            metadata={"user_id": str(user_id)},
        )
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(str(exc)) from exc
    return transfer.id


def platform_balance() -> dict:
    """What's actually in the Stripe balance — the pot member payouts come from.

    Returns None values rather than raising when Stripe can't be reached, so an
    admin page never breaks over a balance lookup.
    """
    stripe.api_key = _secret_key()
    try:
        bal = stripe.Balance.retrieve().to_dict()
    except Exception:  # noqa: BLE001 — informational only
        return {"available": None, "pending": None}
    def total(bucket):
        return round(sum(b.get("amount", 0) for b in bal.get(bucket, [])
                         if b.get("currency") == CURRENCY) / 100.0, 2)
    return {"available": total("available"), "pending": total("pending")}
