"""Admin view of member withdrawals (Part 2).

Shows every payout and — the part that matters operationally — whether the
Stripe balance can cover what members are currently owed. Member payouts are
funded by merchant top-ups sitting in that balance, so a shortfall here is an
early warning that withdrawals are about to start failing.

Admin-gated (reuses the campaigns X-Admin-Key).
"""
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from ..cashback import effective_status, parse_post_ts
from ..db import get_session
from ..models import (AdminPayoutOut, AdminPayoutsOut, Mention, Payout,
                      Receipt, User)
from ..payments import platform_balance
from .campaigns import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/payouts", response_model=AdminPayoutsOut,
            dependencies=[Depends(require_admin)])
def list_payouts(limit: int = Query(default=100, ge=1, le=500),
                 session: Session = Depends(get_session)):
    rows = session.exec(
        select(Payout).order_by(Payout.id.desc()).limit(limit)).all()
    users = {u.id: u for u in session.exec(select(User)).all()}

    out = []
    for p in rows:
        u = users.get(p.user_id)
        name = f"{u.first_name} {u.last_name}".strip() if u else "(deleted member)"
        out.append(AdminPayoutOut(
            id=p.id, userId=p.user_id, userName=name or (u.email if u else ""),
            userEmail=u.email if u else "", amount=p.amount, status=p.status,
            createdAt=p.created_at, failureReason=p.failure_reason))

    # What members could withdraw right now, using the same rule the member
    # dashboard uses, so the two never disagree.
    receipts = session.exec(select(Receipt)).all()
    posts = session.exec(select(Mention)).all()
    ts = {m.id: parse_post_ts(m.timestamp) for m in posts}
    wallet_owed = round(sum(r.amount for r in receipts
                            if effective_status(r, ts.get(r.post_id)) == "confirmed"), 2)

    bal = platform_balance()
    available = bal["available"]
    shortfall = round(max(0.0, wallet_owed - available), 2) if available is not None else 0.0

    return AdminPayoutsOut(
        balanceAvailable=available, balancePending=bal["pending"],
        walletOwed=wallet_owed, shortfall=shortfall,
        totalPaidOut=round(sum(p.amount for p in rows if p.status in ("sent", "paid")), 2),
        failedCount=sum(1 for p in rows if p.status == "failed"),
        payouts=out,
    )
