"""Stripe webhook — where a merchant top-up actually credits the balance.

Stripe calls POST /stripe/webhook server-to-server after a payment. We verify
the signature (so only genuine Stripe callbacks are trusted), and on a paid
`checkout.session.completed` we record the top-up. This is deliberately the ONLY
place a Stripe top-up is credited — the browser redirect back to the site is
never trusted, since it can be faked or dropped.
"""
import json

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from ..db import get_session
from ..models import Merchant, MerchantTransaction
from ..payments import webhook_secret

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
    if event.get("type") == "checkout.session.completed":
        _credit_topup(event["data"]["object"], session)

    # 200 for everything else too, so Stripe doesn't retry events we ignore.
    return {"received": True}


def _credit_topup(cs: dict, session: Session) -> None:
    """Record the top-up for one completed Checkout Session (idempotently)."""
    if cs.get("payment_status") != "paid":
        return
    meta = cs.get("metadata") or {}
    if meta.get("kind") != "topup":
        return
    try:
        merchant_id = int(meta.get("merchant_id"))
    except (TypeError, ValueError):
        return

    ref = cs.get("id")
    # Stripe retries webhooks until it gets a 200 — never credit the same
    # session twice.
    if session.exec(select(MerchantTransaction)
                    .where(MerchantTransaction.stripe_ref == ref)).first():
        return
    if session.get(Merchant, merchant_id) is None:   # account deleted meanwhile
        return

    amount = round((cs.get("amount_total") or 0) / 100.0, 2)   # pence -> pounds
    if amount <= 0:
        return
    session.add(MerchantTransaction(
        merchant_id=merchant_id, kind="topup", amount=amount,
        description="Card top-up", stripe_ref=ref,
    ))
    session.commit()
