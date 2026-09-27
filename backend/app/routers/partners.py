"""Merchant partnership applications.

Brands submit the short partnership form on contact.html (a public POST —
merchants aren't Cirqle users): first name, work email, phone and how they
heard about us. The admin reviews them on admin.html; approving creates the
merchant login and emails the invite in one step. Nothing is published on
approval any more — the brand fills in its details in the portal and submits
its own deal for review. Reads/approve/reject/delete are admin-gated, reusing
the campaigns admin key (X-Admin-Key header).
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlmodel import Session, select

from ..activity import log_activity
from ..db import get_session
from ..models import (Merchant, MerchantApplication, MerchantApplicationIn,
                      MerchantApplicationOut)
from ..ratelimit import rate_limit
from .campaigns import require_admin
from .merchant import create_login_for_application

router = APIRouter(prefix="/partners", tags=["partners"])


def _app_out(a: MerchantApplication, has_login: bool = False) -> MerchantApplicationOut:
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
        hasLogin=has_login,
    )


@router.post("", response_model=MerchantApplicationOut, status_code=201,
             dependencies=[rate_limit("partner_application", limit=5, window=3600)])
def submit_application(data: MerchantApplicationIn,
                       session: Session = Depends(get_session)):
    """Public: a brand submits the partnership form. Stored as 'pending'."""
    enquiry = data.kind == "enquiry"
    if not data.firstName.strip():
        raise HTTPException(status_code=422, detail="Please give us your first name.")
    if enquiry and not data.message.strip():
        raise HTTPException(status_code=422, detail="Please write your question.")
    if not enquiry and not data.phone.strip():
        raise HTTPException(status_code=422, detail="Please give us a phone number.")
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
        kind="enquiry" if enquiry else "application",
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
    with_login = {mid for mid in session.exec(
        select(Merchant.application_id).where(Merchant.application_id.is_not(None))).all()}
    return [_app_out(a, a.id in with_login) for a in session.exec(stmt).all()]


@router.post("/{app_id}/approve", response_model=MerchantApplicationOut,
             dependencies=[Depends(require_admin)])
def approve_application(app_id: int, session: Session = Depends(get_session)):
    """Admin: approve -> create the merchant login and email the invite."""
    a = session.get(MerchantApplication, app_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    if a.kind == "enquiry":
        raise HTTPException(status_code=400,
                            detail="That's a question, not an application — reply to them by email.")
    if a.status == "approved":
        raise HTTPException(status_code=400, detail="Already approved.")

    # Login first: if it fails (say, the email already has a login) the
    # application stays pending rather than approved with no way in.
    created = create_login_for_application(session, a)

    a.status = "approved"
    a.reviewed_at = datetime.utcnow()
    session.add(a)
    session.commit()
    session.refresh(a)
    who = a.brand or f"{a.first_name} ({a.email})"
    log_activity(session, "Approved merchant application", f"{who} → login invited")
    out = _app_out(a, has_login=True)
    out.inviteSent = created.inviteSent
    return out


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
