"""Receipt / cashback-claim endpoints (Phase 4 + 5).

A receipt is a private photo proving a purchase for a deal. It doubles as a
cashback claim: it carries the deal's brand + £ amount (snapshotted at upload)
and a status (pending -> confirmed -> paid / rejected). The dashboard's money is
derived from these rows (see routers/account.py).

Reads/writes of a user's own receipts are auth'd; verify/reject and the review
list are admin-gated (reusing the campaigns admin key).
"""
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from ..activity import log_activity
from ..cashback import admin_status, clears_at, effective_status, parse_post_ts
from ..db import get_session
from ..models import (AdminBulkVerifyIn, AdminBulkVerifyOut, AdminReceiptOut,
                      Campaign, Mention, Receipt, ReceiptOut, User)
from ..security import get_current_user
from ..storage import (StorageError, StorageUploadError, receipt_view_url,
                       upload_receipt)
from .campaigns import require_admin

router = APIRouter(prefix="/receipts", tags=["receipts"])


def _earn_to_amount(earn: str) -> float:
    """'£13.00' -> 13.0 ; '£0.90' -> 0.9 ; '' -> 0.0."""
    nums = re.findall(r"[\d.]+", earn or "")
    try:
        return float(nums[0]) if nums else 0.0
    except ValueError:
        return 0.0


def _receipt_out(r: Receipt, status: Optional[str] = None, with_image: bool = False) -> ReceiptOut:
    return ReceiptOut(
        id=r.id, postId=r.post_id, campaignId=r.campaign_id,
        brand=r.brand, amount=r.amount, status=status or r.status, uploadedAt=r.uploaded_at,
        imageUrl=receipt_view_url(r.image_key) if with_image else None,
    )


def _post_ts_map(user_id: int, session: Session) -> dict:
    """post_id -> parsed post date for this user's stored posts. The 3-day
    cashback clearing counter runs from the post date."""
    mentions = session.exec(select(Mention).where(Mention.user_id == user_id)).all()
    return {m.id: parse_post_ts(m.timestamp) for m in mentions}


@router.get("", response_model=list[ReceiptOut])
def list_receipts(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """This user's receipts/claims, each with a short-lived URL to view their
    own receipt image (presigned; None in local mode)."""
    rows = session.exec(select(Receipt).where(Receipt.user_id == user.id)).all()
    ts = _post_ts_map(user.id, session)
    return [_receipt_out(r, effective_status(r, ts.get(r.post_id)), with_image=True)
            for r in rows]


@router.post("", response_model=ReceiptOut, status_code=201)
def create_receipt(
    post_id: str = Form(...),
    campaign_id: Optional[int] = Form(None),
    image: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Upload a receipt for one of this user's posts, tied to a deal.

    One receipt per (user, post): re-uploading replaces it and resets to pending.
    """
    if not post_id.strip():
        raise HTTPException(status_code=422, detail="post_id is required.")
    try:
        key, digest = upload_receipt(image)
    except StorageUploadError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Snapshot the deal's brand + cashback amount so the claim is self-contained.
    brand, amount = "", 0.0
    if campaign_id is not None:
        camp = session.get(Campaign, campaign_id)
        if camp:
            brand = camp.brand
            amount = _earn_to_amount(camp.earn)

    # Cashback is NOT confirmed on upload. The claim stays 'pending' and clears
    # automatically 3 days after the post date (see app/cashback.py); admin can
    # reject it within that window.
    existing = session.exec(
        select(Receipt).where(Receipt.user_id == user.id, Receipt.post_id == post_id)
    ).first()
    if existing:
        existing.image_key = key
        existing.image_sha256 = digest
        existing.campaign_id = campaign_id
        existing.brand = brand
        existing.amount = amount
        existing.status = "pending"
        existing.uploaded_at = datetime.now(timezone.utc)
        receipt = existing
    else:
        receipt = Receipt(
            user_id=user.id, post_id=post_id, campaign_id=campaign_id,
            brand=brand, amount=amount, image_key=key, image_sha256=digest,
            status="pending",
        )

    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    mention = session.get(Mention, post_id)
    post_ts = parse_post_ts(mention.timestamp) if mention else None
    return _receipt_out(receipt, effective_status(receipt, post_ts))


# ── Admin review (verify cashback claims) ──
@router.get("/admin", response_model=list[AdminReceiptOut],
            dependencies=[Depends(require_admin)])
def admin_list_receipts(
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """All receipts (optionally filtered by status), newest first, with a
    short-lived image URL for review."""
    query = select(Receipt, User).where(Receipt.user_id == User.id)
    if status:
        query = query.where(Receipt.status == status)
    rows = session.exec(query.order_by(Receipt.uploaded_at.desc())).all()
    out = []
    for r, u in rows:
        m = session.get(Mention, r.post_id)
        st = admin_status(r, parse_post_ts(m.timestamp) if m else None)
        out.append(AdminReceiptOut(
            id=r.id, userEmail=u.email, userName=f"{u.first_name} {u.last_name}",
            postId=r.post_id, brand=r.brand, amount=r.amount, status=st,
            uploadedAt=r.uploaded_at, imageUrl=receipt_view_url(r.image_key),
        ))
    return out


def _post_ts_of(r: Receipt, session: Session) -> Optional[datetime]:
    m = session.get(Mention, r.post_id)
    return parse_post_ts(m.timestamp) if m else None


@router.post("/{receipt_id}/verify", response_model=ReceiptOut,
             dependencies=[Depends(require_admin)])
def verify_receipt(receipt_id: int, session: Session = Depends(get_session)):
    """Admin: approve a claim. Its cashback is released to the member's wallet at
    the END of the 3-day window (not now). Approval is only possible while the
    window is open — once the 3 days pass, an unapproved claim expires."""
    r = session.get(Receipt, receipt_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    post_ts = _post_ts_of(r, session)
    if datetime.utcnow() >= clears_at(r, post_ts):
        raise HTTPException(status_code=400,
                            detail="This claim's 3-day window has passed and can no longer be approved.")
    r.status = "verified"
    session.add(r)
    session.commit()
    session.refresh(r)
    log_activity(session, "Approved receipt claim", f"{r.brand or 'Cashback'} £{r.amount:.2f}")
    return _receipt_out(r, effective_status(r, post_ts))


@router.post("/admin/bulk-verify", response_model=AdminBulkVerifyOut,
             dependencies=[Depends(require_admin)])
def bulk_verify_receipts(data: AdminBulkVerifyIn,
                         session: Session = Depends(get_session)):
    """Admin: approve several claims at once.

    Each claim is checked individually against the same 3-day rule as the single
    approve, so an expired one is reported back instead of failing the whole
    batch. One activity-log line covers the batch.
    """
    if not data.ids:
        raise HTTPException(status_code=422, detail="No claims selected.")
    if len(data.ids) > 200:
        raise HTTPException(status_code=422, detail="Too many claims in one batch (max 200).")

    approved, errors = [], []
    for receipt_id in dict.fromkeys(data.ids):     # de-duplicate, keep order
        r = session.get(Receipt, receipt_id)
        if r is None:
            errors.append(f"#{receipt_id}: not found")
            continue
        if r.status not in ("pending", "verified"):
            errors.append(f"#{receipt_id}: already {r.status}")
            continue
        if datetime.utcnow() >= clears_at(r, _post_ts_of(r, session)):
            errors.append(f"#{receipt_id}: 3-day window has passed")
            continue
        r.status = "verified"
        session.add(r)
        approved.append(r)

    if approved:
        session.commit()
        total = sum(r.amount for r in approved)
        log_activity(session, f"Approved {len(approved)} receipt claims",
                     f"£{total:.2f} released across {len(approved)} claim(s)")

    return AdminBulkVerifyOut(approved=len(approved), failed=len(errors), errors=errors[:20])


@router.post("/{receipt_id}/reject", response_model=ReceiptOut,
             dependencies=[Depends(require_admin)])
def reject_receipt(receipt_id: int, session: Session = Depends(get_session)):
    """Admin: reject a claim (no cashback)."""
    r = session.get(Receipt, receipt_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    r.status = "rejected"
    session.add(r)
    session.commit()
    session.refresh(r)
    log_activity(session, "Rejected receipt claim", f"{r.brand or 'Cashback'} £{r.amount:.2f}")
    return _receipt_out(r, "rejected")
