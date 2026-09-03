"""Account dashboard endpoints (Phase 5).

Real per-user numbers for the "My Account" page, all derived from the user's
receipts (the cashback ledger) + their stored posts. No placeholders.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from .. import referrals
from ..cashback import effective_status, parse_post_ts
from ..db import get_session
from ..models import (AccountStats, ActivityItem, Mention, OnboardLinkOut,
                      Payout, PayoutOut, PayoutStatusOut, Receipt,
                      ReferralItemOut, User)
from ..payments import (MIN_PAYOUT, PaymentError, connect_status,
                        create_onboarding_link, create_transfer,
                        ensure_connect_account, payments_configured)
from ..security import get_current_user
from ..storage import receipt_view_url

router = APIRouter(prefix="/account", tags=["account"])


def _compute_stats(user: User, session: Session) -> AccountStats:
    # Referrals clear by the clock, so there's no moment a job could catch. Bank
    # anything newly earned before we add the money up (see app/referrals.py).
    referrals.settle_for_user(user, session)

    receipts = session.exec(select(Receipt).where(Receipt.user_id == user.id)).all()
    posts = session.exec(select(Mention).where(Mention.user_id == user.id)).all()

    # Cashback status is time-based: a claim clears to 'confirmed' 3 days after
    # its post date (app/cashback.py), so we compute the effective status here.
    ts = {m.id: parse_post_ts(m.timestamp) for m in posts}
    def eff(r: Receipt) -> str:
        return effective_status(r, ts.get(r.post_id))

    # Referral bonuses — their own £1s for referring, plus 50p for having been
    # referred — sit alongside cashback in the same wallet (one balance, one
    # withdrawal) but stay their own rows so the two can still be told apart.
    reward_available, reward_paid, reward_count = referrals.totals(user.id, session)

    pending = round(sum(r.amount for r in receipts if eff(r) == "pending"), 2)
    wallet = round(sum(r.amount for r in receipts if eff(r) == "confirmed")
                   + reward_available, 2)                    # available to withdraw
    paid = round(sum(r.amount for r in receipts if r.status == "paid")
                 + reward_paid, 2)
    earned = round(wallet + paid, 2)     # all cleared cashback + bonuses ever

    brands = {r.brand for r in receipts if r.brand}
    recent = sorted(receipts, key=lambda r: r.uploaded_at, reverse=True)[:6]

    return AccountStats(
        totalEarned=earned,
        pending=pending,
        wallet=wallet,
        paidOut=paid,
        brandsUsed=len(brands),
        postsCount=len(posts),
        receiptsCount=len(receipts),
        referralEarnings=round(reward_available + reward_paid, 2),
        referralCount=reward_count,
        activity=[
            ActivityItem(brand=r.brand or "Cashback", amount=r.amount,
                         status=eff(r), date=r.uploaded_at,
                         imageUrl=receipt_view_url(r.image_key))
            for r in recent
        ],
    )


@router.get("/stats", response_model=AccountStats)
def get_stats(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The user's real dashboard figures."""
    return _compute_stats(user, session)


def _wallet(user: User, session: Session) -> tuple[float, list, list]:
    """£ cleared and withdrawable, plus the receipts and referral rewards that
    make it up. Both are needed by `withdraw`, which marks each one paid."""
    referrals.settle_for_user(user, session)

    receipts = session.exec(select(Receipt).where(Receipt.user_id == user.id)).all()
    posts = session.exec(select(Mention).where(Mention.user_id == user.id)).all()
    ts = {m.id: parse_post_ts(m.timestamp) for m in posts}
    ready = [r for r in receipts
             if effective_status(r, ts.get(r.post_id)) == "confirmed"]
    rewards = referrals.available_rewards(user.id, session)
    total = round(sum(r.amount for r in ready) + sum(w.amount for w in rewards), 2)
    return total, ready, rewards


def _sync_connect(user: User, session: Session) -> None:
    """Refresh our copy of whether Stripe will let us pay this member."""
    if not user.stripe_account_id:
        return
    try:
        status = connect_status(user.stripe_account_id)
    except PaymentError:
        return                          # keep the last known state
    user.payouts_enabled = status["payouts_enabled"]
    user.payout_details_submitted = status["details_submitted"]
    # Hashes of what Stripe verified, kept so two accounts belonging to one
    # person can't refer each other. Only overwritten when Stripe actually
    # returns something, so a partial response can't wipe a known fingerprint.
    if status.get("payout_fingerprint"):
        user.payout_fingerprint = status["payout_fingerprint"]
    if status.get("identity_fingerprint"):
        user.identity_fingerprint = status["identity_fingerprint"]
    session.add(user)
    session.commit()
    session.refresh(user)


@router.get("/payouts/status", response_model=PayoutStatusOut)
def payout_status(user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    """Can this member cash out yet, and if not, why not?"""
    _sync_connect(user, session)
    wallet, _receipts, _rewards = _wallet(user, session)
    ready = bool(user.payouts_enabled)
    if not payments_configured():
        reason = "Withdrawals aren't available right now."
    elif not user.stripe_account_id or not user.payout_details_submitted:
        reason = "Verify your identity and add a bank account to withdraw."
    elif not ready:
        reason = "Stripe is still checking your details — this usually takes a few minutes."
    elif wallet < MIN_PAYOUT:
        reason = f"You need at least £{MIN_PAYOUT:.2f} to withdraw."
    else:
        reason = ""
    return PayoutStatusOut(
        ready=ready, detailsSubmitted=bool(user.payout_details_submitted),
        wallet=wallet, minimum=MIN_PAYOUT,
        canWithdraw=ready and wallet >= MIN_PAYOUT and payments_configured(),
        reason=reason,
    )


@router.get("/referrals", response_model=list[ReferralItemOut])
def list_referrals(user: User = Depends(get_current_user),
                   session: Session = Depends(get_session)):
    """Every referral this member is part of, newest first — both the people
    they referred (£1 each) and their own referral, if they were referred (50p).

    Includes the ones that haven't paid out, each with the reason — a bonus that
    quietly never arrives is worse than one that says what it's waiting for.
    """
    referrals.settle_for_user(user, session)
    return [ReferralItemOut(**item) for item in referrals.summary(user, session)]


@router.post("/payouts/onboard", response_model=OnboardLinkOut)
def payout_onboard(request: Request, user: User = Depends(get_current_user),
                   session: Session = Depends(get_session)):
    """Start (or resume) Stripe's hosted identity + bank details form.

    Stripe collects and stores everything; we never handle bank details.
    """
    if not payments_configured():
        raise HTTPException(status_code=503, detail="Withdrawals aren't available right now.")
    try:
        account_id = ensure_connect_account(user, session)
        url = create_onboarding_link(account_id, request.headers.get("origin", ""))
    except PaymentError:
        raise HTTPException(status_code=502,
                            detail="Could not start verification. Please try again.")
    return OnboardLinkOut(url=url)


@router.get("/payouts", response_model=list[PayoutOut])
def list_payouts(user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    """This member's withdrawal history, newest first."""
    rows = session.exec(
        select(Payout).where(Payout.user_id == user.id)
        .order_by(Payout.id.desc())).all()
    return [PayoutOut(id=p.id, amount=p.amount, status=p.status,
                      createdAt=p.created_at, failureReason=p.failure_reason)
            for p in rows]


@router.post("/withdraw", response_model=AccountStats)
def withdraw(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Send the member's cleared cashback to their bank via Stripe.

    Order matters: the money moves FIRST, and only a successful transfer marks
    the receipts paid. Doing it the other way round would show a member as paid
    whenever Stripe failed.
    """
    if not payments_configured():
        raise HTTPException(status_code=503, detail="Withdrawals aren't available right now.")
    _sync_connect(user, session)
    if not user.payouts_enabled:
        raise HTTPException(
            status_code=409,
            detail="Verify your identity and add a bank account before withdrawing.")

    amount, ready, rewards = _wallet(user, session)
    if amount < MIN_PAYOUT:
        raise HTTPException(status_code=422,
                            detail=f"You need at least £{MIN_PAYOUT:.2f} to withdraw.")

    try:
        transfer_id = create_transfer(user.stripe_account_id, amount, user.id)
    except PaymentError as exc:
        # Most likely the platform balance can't cover it yet. Nothing is marked
        # paid, so the member's wallet is untouched and they can retry.
        session.add(Payout(user_id=user.id, amount=amount, status="failed",
                           failure_reason=str(exc)[:400]))
        session.commit()
        raise HTTPException(status_code=502,
                            detail="Withdrawal couldn't be sent right now. Please try again shortly.")

    for r in ready:
        r.status = "paid"
        session.add(r)
    # Referral bonuses go out in the same transfer, so they're marked paid by
    # the same success. Missing this would leave them withdrawable forever.
    for reward in rewards:
        reward.status = "paid"
        session.add(reward)
    session.add(Payout(user_id=user.id, amount=amount, status="sent",
                       stripe_transfer_id=transfer_id))
    session.commit()
    return _compute_stats(user, session)
