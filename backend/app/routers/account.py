"""Account dashboard endpoints (Phase 5).

Real per-user numbers for the "My Account" page, all derived from the user's
receipts (the cashback ledger) + their stored posts. No placeholders.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..cashback import effective_status, parse_post_ts
from ..db import get_session
from ..models import AccountStats, ActivityItem, Mention, Receipt, User
from ..security import get_current_user
from ..storage import receipt_view_url

router = APIRouter(prefix="/account", tags=["account"])


def _compute_stats(user: User, session: Session) -> AccountStats:
    receipts = session.exec(select(Receipt).where(Receipt.user_id == user.id)).all()
    posts = session.exec(select(Mention).where(Mention.user_id == user.id)).all()

    # Cashback status is time-based: a claim clears to 'confirmed' 3 days after
    # its post date (app/cashback.py), so we compute the effective status here.
    ts = {m.id: parse_post_ts(m.timestamp) for m in posts}
    def eff(r: Receipt) -> str:
        return effective_status(r, ts.get(r.post_id))

    pending = round(sum(r.amount for r in receipts if eff(r) == "pending"), 2)
    wallet = round(sum(r.amount for r in receipts if eff(r) == "confirmed"), 2)  # available to withdraw
    paid = round(sum(r.amount for r in receipts if r.status == "paid"), 2)
    earned = round(wallet + paid, 2)     # all cleared cashback ever

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


@router.post("/withdraw", response_model=AccountStats)
def withdraw(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Withdraw the cleared balance — marks effectively-confirmed claims as paid.

    DISABLED until real payouts exist (Part 2: Stripe Connect). Marking claims
    "paid" while no money leaves Cirqle would tell members — and the admin
    ledger — that they'd been paid when they hadn't. The dashboard already
    shows "withdrawals coming soon"; this makes the API agree.
    """
    raise HTTPException(
        status_code=503,
        detail="Withdrawals aren't live yet — your balance is safe and stays put.")
    receipts = session.exec(select(Receipt).where(Receipt.user_id == user.id)).all()
    posts = session.exec(select(Mention).where(Mention.user_id == user.id)).all()
    ts = {m.id: parse_post_ts(m.timestamp) for m in posts}
    for r in receipts:
        if effective_status(r, ts.get(r.post_id)) == "confirmed":
            r.status = "paid"
            session.add(r)
    session.commit()
    return _compute_stats(user, session)
