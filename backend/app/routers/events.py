"""Deal-event tracking (Phase 6).

A tiny public endpoint that logs a deal view or an outbound click. deal.html
fires these via navigator.sendBeacon; the merchant dashboard aggregates them.
Anonymous — no auth, no user_id — so it stays cheap and privacy-light.
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session

from ..db import get_session
from ..models import Campaign, DealEvent, EventIn
from ..ratelimit import rate_limit

router = APIRouter(prefix="/events", tags=["events"])

_ALLOWED_KINDS = ("view", "click")


# Anonymous, so without a cap anyone could inflate a merchant's view/click stats.
@router.post("", status_code=204, dependencies=[rate_limit("events", limit=60, window=600)])
def log_event(data: EventIn, session: Session = Depends(get_session)):
    """Record a view/click for a deal. 204 on success (nothing to return)."""
    if data.kind not in _ALLOWED_KINDS:
        raise HTTPException(status_code=422, detail="Unknown event kind.")
    if session.get(Campaign, data.campaignId) is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    session.add(DealEvent(campaign_id=data.campaignId, kind=data.kind))
    session.commit()
    return Response(status_code=204)
