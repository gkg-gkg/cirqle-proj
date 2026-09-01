"""Campaign catalog endpoints (Phase 3).

Campaigns are the global cashback "deals" shown to everyone (formerly hardcoded
in browse.html / deal.html). Reads are public; writes are gated by an admin key
(there's no merchant persona yet — see CIRQLE_ADMIN_KEY).

Create/edit are multipart/form-data: a `payload` JSON field carries the text
fields, and `images` carries the uploaded photo files (handed to storage.py,
which puts them on S3 in prod or backend/media/ locally).
"""
import json
import os

from fastapi import (APIRouter, Depends, File, Form, Header, HTTPException,
                     Response, UploadFile)
from sqlmodel import Session, select

from ..activity import log_activity
from ..db import get_session
from ..models import (Campaign, CampaignIn, CampaignOut, CampaignSubmission,
                      DealEvent, MerchantApplication, Receipt)
from ..storage import StorageError, StorageUploadError, delete_image, upload_image

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

# Shared secret for writes. Defaults to a dev value (like CIRQLE_SECRET_KEY);
# set a real one in prod .env.
ADMIN_KEY = os.environ.get("CIRQLE_ADMIN_KEY", "dev-admin-key")

# CampaignIn (camelCase) field -> Campaign column (snake_case). `tags`/`images`
# are handled separately (list <-> JSON string).
_FIELD_MAP = {
    "brand": "brand",
    "title": "title",
    "cardTitle": "card_title",
    "cardDesc": "card_desc",
    "longDesc": "long_desc",
    "emoji": "emoji",
    "category": "category",
    "rate": "rate",
    "earn": "earn",
    "spendDesc": "spend_desc",
    "totalPaid": "total_paid",
    "members": "members",
    "claims": "claims",
    "expiry": "expiry",
    "location": "location",
    "terms": "terms",
    "brandUrl": "brand_url",
    "bg": "bg",
    "cashbackMode": "cashback_mode",
    "baseCashback": "base_cashback",
    "expectedEngagementBaseline": "expected_engagement_baseline",
    "maxMultiplier": "max_multiplier",
    "perPostCap": "per_post_cap",
    "budgetTotal": "budget_total",
}


def require_admin(x_admin_key: str = Header(default="")):
    """Gate write endpoints behind the shared admin key (X-Admin-Key header)."""
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing admin key.")


def _campaign_out(c: Campaign) -> CampaignOut:
    """A stored row -> the shape the browse/deal pages render."""
    return CampaignOut(
        id=c.id,
        brand=c.brand,
        title=c.title,
        cardTitle=c.card_title,
        cardDesc=c.card_desc,
        longDesc=c.long_desc,
        emoji=c.emoji,
        category=c.category,
        rate=c.rate,
        earn=c.earn,
        spendDesc=c.spend_desc,
        totalPaid=c.total_paid,
        members=c.members,
        claims=c.claims,
        expiry=c.expiry,
        location=c.location,
        terms=c.terms,
        brandUrl=c.brand_url,
        bg=c.bg,
        tags=json.loads(c.tags or "[]"),
        images=json.loads(c.images or "[]"),
        cashbackMode=c.cashback_mode,
        baseCashback=c.base_cashback,
        expectedEngagementBaseline=c.expected_engagement_baseline,
        maxMultiplier=c.max_multiplier,
        perPostCap=c.per_post_cap,
        budgetTotal=c.budget_total,
        budgetRemaining=c.budget_remaining,
    )


def _parse_payload(payload: str) -> CampaignIn:
    try:
        return CampaignIn.model_validate_json(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid campaign data: {exc}")


def _apply_in(c: Campaign, data: CampaignIn) -> None:
    """Copy the provided fields from a CampaignIn onto a Campaign row.

    Uses exclude_unset so PATCH only touches fields that were actually sent.
    """
    provided = data.model_dump(exclude_unset=True)
    for camel, column in _FIELD_MAP.items():
        if provided.get(camel) is not None:
            setattr(c, column, provided[camel])
    if provided.get("tags") is not None:
        c.tags = json.dumps(data.tags)


def _upload_all(images: list[UploadFile]) -> list[str]:
    """Store each uploaded image, returning the list of public URLs."""
    urls: list[str] = []
    for f in images:
        if f is None or not f.filename:
            continue  # browsers can send empty file parts
        try:
            urls.append(upload_image(f))
        except StorageUploadError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except StorageError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return urls


@router.get("", response_model=list[CampaignOut])
def list_campaigns(session: Session = Depends(get_session)):
    """Public: every campaign, oldest first (feeds browse.html)."""
    rows = session.exec(select(Campaign).order_by(Campaign.id)).all()
    return [_campaign_out(c) for c in rows]


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: int, session: Session = Depends(get_session)):
    """Public: one campaign (feeds deal.html)."""
    c = session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    return _campaign_out(c)


@router.post("", response_model=CampaignOut, status_code=201,
             dependencies=[Depends(require_admin)])
def create_campaign(
    payload: str = Form(...),
    images: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
):
    """Admin: create a campaign (text payload + up to 5 image files)."""
    data = _parse_payload(payload)
    if not (data.brand and data.title):
        raise HTTPException(status_code=422, detail="brand and title are required.")

    c = Campaign(images=json.dumps(_upload_all(images)))
    _apply_in(c, data)
    if data.budgetTotal is not None:
        c.budget_remaining = data.budgetTotal
    session.add(c)
    session.commit()
    session.refresh(c)
    return _campaign_out(c)


@router.patch("/{campaign_id}", response_model=CampaignOut,
              dependencies=[Depends(require_admin)])
def update_campaign(
    campaign_id: int,
    payload: str = Form(...),
    images: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
):
    """Admin: edit a campaign. Images are replaced only if new files are sent."""
    c = session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    data = _parse_payload(payload)
    old_budget_total = c.budget_total
    _apply_in(c, data)
    if data.budgetTotal is not None:
        # Adjust remaining by the delta so topping up mid-campaign doesn't
        # reset spend already tracked against it.
        delta = data.budgetTotal - (old_budget_total or 0)
        c.budget_remaining = (c.budget_remaining or 0) + delta
    real_images = [f for f in images if f and f.filename]
    old_images: list[str] = []
    if real_images:
        old_images = json.loads(c.images or "[]")
        c.images = json.dumps(_upload_all(real_images))

    session.add(c)
    session.commit()
    session.refresh(c)
    # New images are committed — now the replaced ones can go.
    for url in old_images:
        delete_image(url)
    return _campaign_out(c)


@router.delete("/{campaign_id}", status_code=204,
               dependencies=[Depends(require_admin)])
def delete_campaign(campaign_id: int, session: Session = Depends(get_session)):
    """Admin: delete a campaign.

    First unlinks anything that references it — receipts (which keep their
    snapshotted brand/amount), merchant applications, and campaign submissions —
    so a foreign-key constraint (on Postgres) can't block the delete. Deal
    events have a non-nullable campaign link, so those rows are deleted outright.
    """
    c = session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    for ev in session.exec(select(DealEvent).where(DealEvent.campaign_id == campaign_id)).all():
        session.delete(ev)
    for r in session.exec(select(Receipt).where(Receipt.campaign_id == campaign_id)).all():
        r.campaign_id = None
        session.add(r)
    for a in session.exec(select(MerchantApplication).where(MerchantApplication.campaign_id == campaign_id)).all():
        a.campaign_id = None
        session.add(a)
    for sub in session.exec(select(CampaignSubmission).where(CampaignSubmission.campaign_id == campaign_id)).all():
        sub.campaign_id = None
        session.add(sub)
    session.flush()

    brand = c.brand
    images = json.loads(c.images or "[]")
    session.delete(c)
    session.commit()
    # Campaign row is gone — clear its images out of the bucket too.
    for url in images:
        delete_image(url)
    log_activity(session, "Deleted campaign", f"{brand or 'Campaign'} (deal #{campaign_id})")
    return Response(status_code=204)
