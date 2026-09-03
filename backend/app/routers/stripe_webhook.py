"""Stripe webhook — the single place money changes anything in our database.

Stripe calls POST /stripe/webhook server-to-server after every payment event.
We verify the signature (so only genuine Stripe callbacks are trusted), then act
on the events we care about:

  checkout.session.completed   -> a top-up landed, or a plan was started
  customer.subscription.*      -> plan/tier/status/renewal date changed
  invoice.paid                 -> a monthly membership fee was charged
  invoice.payment_failed       -> card failed; the portal shows a warning
  charge.refunded              -> credit returned to their card
  account.updated              -> a member finished (or failed) identity checks
  transfer.reversed            -> a member payout bounced back

Nothing here trusts the browser: the redirect back to the site can be faked or
dropped, so crediting only ever happens on a signed event.
"""
import json
from datetime import datetime
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from ..db import get_session
from ..models import Merchant, MerchantTransaction, Payout, User
from ..payments import TIERS, webhook_secret

try:                                    # stripe <15 kept exceptions under .error
    from stripe.error import SignatureVerificationError
except Exception:                       # stripe >=15 exposes it at the top level
    from stripe import SignatureVerificationError

router = APIRouter(prefix="/stripe", tags=["stripe"])


@router.post("/webhook")
async def stripe_webhook(request: Request, session: Session = Depends(get_session)):
    secret = webhook_secret()
    if not secret:
        # Misconfiguration, not the caller's fault — but we can't trust anything
        # unsigned, so refuse rather than credit blindly.
        raise HTTPException(status_code=503, detail="Webhook not configured.")

    payload = await request.body()      # raw bytes — needed to verify the signature
    sig = request.headers.get("stripe-signature", "")
    try:
        stripe.Webhook.construct_event(payload, sig, secret)   # verify only
    except (ValueError, SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    # Signature is good — work from the raw JSON (plain dicts, not StripeObjects).
    event = json.loads(payload)
    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed":
        _checkout_completed(obj, session)
    elif kind in ("customer.subscription.created", "customer.subscription.updated",
                  "customer.subscription.deleted"):
        _sync_subscription(obj, session, deleted=kind.endswith("deleted"))
    elif kind == "invoice.paid":
        _invoice_paid(obj, session)
    elif kind == "invoice.payment_failed":
        _set_status_by_customer(obj.get("customer"), "past_due", session)
    elif kind == "charge.refunded":
        _charge_refunded(obj, session)
    elif kind == "account.updated":
        _sync_connect_account(obj, session)
    elif kind == "transfer.reversed":
        _transfer_reversed(obj, session)

    # 200 for everything else too, so Stripe doesn't retry events we ignore.
    return {"received": True}


# ── helpers ──────────────────────────────────────────────────────────────────
def _merchant_by_customer(customer_id, session) -> Optional[Merchant]:
    if not customer_id:
        return None
    return session.exec(
        select(Merchant).where(Merchant.stripe_customer_id == customer_id)).first()


def _already_recorded(ref: str, session) -> bool:
    """Stripe retries webhooks until it gets a 200 — never record the same
    payment twice."""
    return bool(ref) and bool(session.exec(
        select(MerchantTransaction)
        .where(MerchantTransaction.stripe_ref == ref)).first())


def _checkout_completed(cs: dict, session: Session) -> None:
    """A hosted Checkout finished: either a prepaid top-up or a plan signup."""
    if cs.get("payment_status") != "paid":
        return
    meta = cs.get("metadata") or {}
    if meta.get("kind") == "topup":
        _credit_topup(cs, meta, session)
    # A subscription checkout needs no crediting here — the subscription.created
    # event carries the authoritative plan state and fires alongside this one.


def _credit_topup(cs: dict, meta: dict, session: Session) -> None:
    """Record the credit (and its platform fee) for one completed top-up."""
    try:
        merchant_id = int(meta.get("merchant_id"))
    except (TypeError, ValueError):
        return

    ref = cs.get("id")
    if _already_recorded(ref, session):
        return
    if session.get(Merchant, merchant_id) is None:   # account deleted meanwhile
        return

    # Credit exactly what was quoted; the fee is Cirqle's revenue, not balance.
    try:
        credit = round(float(meta.get("credit")), 2)
        fee = round(float(meta.get("fee", 0) or 0), 2)
    except (TypeError, ValueError):
        # Older sessions had no split — fall back to the whole amount as credit.
        credit, fee = round((cs.get("amount_total") or 0) / 100.0, 2), 0.0
    if credit <= 0:
        return

    # Which pot to credit. Older sessions carry no wallet, and everything before
    # two wallets existed was cashback, so that's the fallback.
    wallet = meta.get("wallet") or "cashback"
    if wallet not in ("cashback", "referral"):
        wallet = "cashback"

    session.add(MerchantTransaction(
        merchant_id=merchant_id, kind="topup", amount=credit, fee=fee,
        wallet=wallet,
        description=("Card top-up — referral wallet" if wallet == "referral"
                     else "Card top-up"),
        stripe_ref=ref,
    ))
    if fee > 0:
        session.add(MerchantTransaction(
            merchant_id=merchant_id, kind="platform_fee", amount=fee,
            wallet=wallet,
            description="Platform fee on credit beyond your monthly allowance",
            stripe_ref=f"{ref}:fee",
        ))
    session.commit()


def _sync_subscription(sub: dict, session: Session, deleted: bool = False) -> None:
    """Mirror Stripe's subscription state onto the merchant row.

    Stripe stays the source of truth; this copy just lets the portal render
    without an API call on every page load.
    """
    merchant = _merchant_by_customer(sub.get("customer"), session)
    if merchant is None:
        return

    tier = (sub.get("metadata") or {}).get("tier", "")
    if tier not in TIERS:
        tier = merchant.tier          # keep what we had if Stripe didn't say
    status = "canceled" if deleted else (sub.get("status") or "none")
    # Stripe's trialing/active both mean "can use the service".
    merchant.subscription_status = "active" if status in ("active", "trialing") else status
    merchant.stripe_subscription_id = "" if deleted else (sub.get("id") or "")
    merchant.tier = "" if deleted else tier
    period_end = sub.get("current_period_end")
    merchant.current_period_end = (
        datetime.utcfromtimestamp(period_end) if period_end and not deleted else None)
    session.add(merchant)
    session.commit()


def _invoice_paid(inv: dict, session: Session) -> None:
    """Log a paid membership fee so it appears in the merchant's own ledger."""
    merchant = _merchant_by_customer(inv.get("customer"), session)
    if merchant is None:
        return
    ref = inv.get("id")
    if _already_recorded(ref, session):
        return
    amount = round((inv.get("amount_paid") or 0) / 100.0, 2)
    if amount <= 0:                    # £0 invoices (proration credits) aren't fees
        return
    tier = TIERS.get(merchant.tier or "")
    session.add(MerchantTransaction(
        merchant_id=merchant.id, kind="subscription", amount=amount,
        description=f"{tier['name']} membership" if tier else "Membership fee",
        stripe_ref=ref,
    ))
    session.commit()


def _set_status_by_customer(customer_id, status: str, session: Session) -> None:
    merchant = _merchant_by_customer(customer_id, session)
    if merchant is None:
        return
    merchant.subscription_status = status
    session.add(merchant)
    session.commit()


def _charge_refunded(charge: dict, session: Session) -> None:
    """A top-up was refunded — take the credit back off their balance."""
    merchant = _merchant_by_customer(charge.get("customer"), session)
    if merchant is None:
        return
    ref = f"refund:{charge.get('id')}:{charge.get('amount_refunded')}"
    if _already_recorded(ref, session):
        return
    refunded = round((charge.get("amount_refunded") or 0) / 100.0, 2)
    if refunded <= 0:
        return
    # Which pot the money came out of. Charges made before two wallets existed
    # carry no metadata, and all of those were cashback.
    wallet = (charge.get("metadata") or {}).get("wallet") or "cashback"
    if wallet not in ("cashback", "referral"):
        wallet = "cashback"
    session.add(MerchantTransaction(
        merchant_id=merchant.id, kind="refund", amount=-refunded, wallet=wallet,
        description=("Refund to card — referral wallet" if wallet == "referral"
                     else "Refund to card"),
        stripe_ref=ref,
    ))
    session.commit()


def _sync_connect_account(acct: dict, session: Session) -> None:
    """A member's Connect account changed — mirror whether they can be paid.

    Stripe's checks finish asynchronously (sometimes minutes after the member
    submits the form), so this is what flips "verifying" to "ready" without
    them having to refresh and wait.
    """
    account_id = acct.get("id")
    if not account_id:
        return
    user = session.exec(
        select(User).where(User.stripe_account_id == account_id)).first()
    if user is None:
        return
    user.payouts_enabled = bool(acct.get("payouts_enabled"))
    user.payout_details_submitted = bool(acct.get("details_submitted"))
    session.add(user)
    session.commit()


def _transfer_reversed(transfer: dict, session: Session) -> None:
    """A payout bounced back — mark it failed so the member and admin can see."""
    payout = session.exec(
        select(Payout).where(Payout.stripe_transfer_id == transfer.get("id"))).first()
    if payout is None or payout.status == "failed":
        return
    payout.status = "failed"
    payout.failure_reason = "The transfer was reversed by Stripe."
    session.add(payout)
    session.commit()
