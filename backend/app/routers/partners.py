"""Merchant partnership applications.

Brands submit the partnership form on contact.html (a public POST — merchants
aren't Cirqle users). The admin reviews them on admin.html and, on approve, a
live `Campaign` (deal) is created from the application's key fields, so the
brand appears on the public Deals page. Reads/approve/reject/delete are
admin-gated, reusing the campaigns admin key (X-Admin-Key header).
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlmodel import Session, select

from ..activity import log_activity
from ..db import get_session
from ..models import (Campaign, MerchantApplication, MerchantApplicationIn,
                      MerchantApplicationOut)
from .campaigns import require_admin

router = APIRouter(prefix="/partners", tags=["partners"])


def _app_out(a: MerchantApplication) -> MerchantApplicationOut:
    """A stored application row -> the shape the admin page renders."""
    return MerchantApplicationOut(
        id=a.id,
        brand=a.brand,
        website=a.website,
        category=a.category,
        cashbackRate=a.cashback_rate,
        markets=a.markets,
        firstName=a.first_name,
        lastName=a.last_name,
        email=a.email,
        phone=a.phone,
        role=a.role,
        revenue=a.revenue,
        orders=a.orders,
        aov=a.aov,
        budget=a.budget,
        timeline=a.timeline,
        goals=json.loads(a.goals or "[]"),
        heard=a.heard,
        message=a.message,
        status=a.status,
        tier=a.tier,
        kind=a.kind,
        campaignId=a.campaign_id,
        createdAt=a.created_at,
    )


@router.post("", response_model=MerchantApplicationOut, status_code=201)
def submit_application(data: MerchantApplicationIn,
                       session: Session = Depends(get_session)):
    """Public: a brand submits the partnership form. Stored as 'pending'."""
    a = MerchantApplication(
        brand=data.brand.strip(),
        website=data.website.strip(),
        category=data.category.strip(),
        cashback_rate=data.cashbackRate,
        markets=data.markets.strip(),
        first_name=data.firstName.strip(),
        last_name=data.lastName.strip(),
        email=str(data.email).strip(),
        phone=data.phone.strip(),
        role=data.role.strip(),
        revenue=data.revenue.strip(),
        orders=data.orders.strip(),
        aov=data.aov.strip(),
        budget=data.budget.strip(),
        timeline=data.timeline.strip(),
        goals=json.dumps(data.goals),
        heard=data.heard.strip(),
        message=data.message.strip(),
        tier=data.tier.strip(),
        # A short "just a question" submission lands in the same inbox, tagged
        # so the admin can tell it from a full application.
        kind="enquiry" if data.kind == "enquiry" else "application",
    )
    session.add(a)
    session.commit()
    session.refresh(a)
    return _app_out(a)


@router.get("", response_model=list[MerchantApplicationOut],
            dependencies=[Depends(require_admin)])
def list_applications(status: str = Query(default=""),
                      session: Session = Depends(get_session)):
    """Admin: list applications, newest first. Optional ?status=pending|approved|rejected."""
    stmt = select(MerchantApplication).order_by(MerchantApplication.id.desc())
    if status:
        stmt = stmt.where(MerchantApplication.status == status)
    return [_app_out(a) for a in session.exec(stmt).all()]


def _campaign_from_application(a: MerchantApplication) -> Campaign:
    """Build a live deal from an approved application's key fields.

    The application always carries brand, category, website and a cashback
    rate; image/description are left as sensible defaults for the admin to
    refine later in the campaign editor.
    """
    rate = a.cashback_rate
    # Derive an example "earn" figure from the avg order value, if provided.
    earn, spend_desc = "", ""
    try:
        aov = float(a.aov)
        if aov > 0:
            earn = f"£{aov * rate / 100:.2f}"
            spend_desc = f"on a £{aov:.0f} spend"
    except (TypeError, ValueError):
        pass
    location = f"Online · {a.markets}" if a.markets else "Online"
    return Campaign(
        brand=a.brand,
        title=f"{a.brand} — up to {rate:g}% cashback",
        card_title=a.brand,
        card_desc=a.message or f"Earn {rate:g}% cashback when you shop at {a.brand}.",
        long_desc=a.message,
        emoji="🛍️",
        category=a.category,
        rate=rate,
        earn=earn,
        spend_desc=spend_desc,
        expiry="Ongoing",
        location=location,
        brand_url=a.website,
    )


@router.post("/{app_id}/approve", response_model=MerchantApplicationOut,
             dependencies=[Depends(require_admin)])
def approve_application(app_id: int, session: Session = Depends(get_session)):
    """Admin: approve -> publish a live deal built from the application."""
    a = session.get(MerchantApplication, app_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    if a.status == "approved":
        raise HTTPException(status_code=400, detail="Already approved.")

    c = _campaign_from_application(a)
    session.add(c)
    session.commit()
    session.refresh(c)

    a.status = "approved"
    a.campaign_id = c.id
    a.reviewed_at = datetime.utcnow()
    session.add(a)
    session.commit()
    session.refresh(a)
    log_activity(session, "Approved merchant application", f"{a.brand} → live deal #{c.id}")
    return _app_out(a)


@router.post("/{app_id}/reject", response_model=MerchantApplicationOut,
             dependencies=[Depends(require_admin)])
def reject_application(app_id: int, session: Session = Depends(get_session)):
    """Admin: reject an application (no deal is created)."""
    a = session.get(MerchantApplication, app_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    a.status = "rejected"
    a.reviewed_at = datetime.utcnow()
    session.add(a)
    session.commit()
    session.refresh(a)
    log_activity(session, "Rejected merchant application", a.brand)
    return _app_out(a)


@router.delete("/{app_id}", status_code=204,
               dependencies=[Depends(require_admin)])
def delete_application(app_id: int, session: Session = Depends(get_session)):
    """Admin: delete an application (does not remove any published deal)."""
    a = session.get(MerchantApplication, app_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    brand = a.brand
    session.delete(a)
    session.commit()
    log_activity(session, "Deleted merchant application", brand)
    return Response(status_code=204)
