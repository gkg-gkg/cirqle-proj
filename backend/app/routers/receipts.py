"""Receipt / cashback-claim endpoints (Phase 4 + 5).

A receipt is a private photo proving a purchase for a deal. It doubles as a
cashback claim: it carries the deal's brand + £ amount (snapshotted at upload)
and a status (pending -> confirmed -> paid / rejected). The dashboard's money is
derived from these rows (see routers/account.py).

Reads/writes of a user's own receipts are auth'd; verify/reject and the review
list are admin-gated (reusing the campaigns admin key).
"""
import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, UploadFile)
from sqlmodel import Session, func, select

from .. import referrals
from ..activity import log_activity
from ..aqs import ALGORITHM_VERSION, compute_aqs, compute_payout
from ..cashback import (APPROVED_STATUSES, admin_status, clears_at,
                        earn_to_amount, effective_status, parse_post_ts)
from ..db import get_session
from ..handles import normalize_handle
from ..models import (AdminBulkVerifyIn, AdminBulkVerifyOut, AdminCheckBucket,
                      AdminCheckCalibration, AdminReceiptOut, AdminReferralOut,
                      AdminVerificationReceiptOut, Campaign, Mention, Receipt,
                      ReceiptOut, ReferralReward, User)
from ..ratelimit import rate_limit
from ..receipt_verification import (AUTO_APPROVAL_ENABLED,
                                    VERIFICATION_AUTO_APPROVE_THRESHOLD,
                                    run_claude_verification,
                                    trigger_cashback_calculation)
from ..security import get_current_user
from ..storage import (StorageError, StorageTooLargeError, StorageUploadError,
                       delete_receipt, receipt_view_url, upload_receipt)
from ..verify import check_receipt, summarise
from .campaigns import require_admin

router = APIRouter(prefix="/receipts", tags=["receipts"])

# Old private name kept so the rest of this file doesn't need touching.
_earn_to_amount = earn_to_amount


def _receipt_out(r: Receipt, status: Optional[str] = None, with_image: bool = False) -> ReceiptOut:
    return ReceiptOut(
        id=r.id, postId=r.post_id, campaignId=r.campaign_id,
        brand=r.brand, amount=r.amount, status=status or r.status, uploadedAt=r.uploaded_at,
        imageUrl=receipt_view_url(r.image_key) if with_image else None,
        referredByHandle=r.referred_by_handle, referralStatus=r.referral_status,
    )


def _post_ts_map(user_id: int, session: Session) -> dict:
    """post_id -> parsed post date for this user's stored posts. The 3-day
    cashback clearing counter runs from the post date."""
    mentions = session.exec(select(Mention).where(Mention.user_id == user_id)).all()
    return {m.id: parse_post_ts(m.timestamp) for m in mentions}


def _resolve_referral(raw_handle: str, campaign_id: Optional[int], user: User,
                      session: Session) -> tuple[Optional[int], str, str]:
    """Check 'referred by @handle' at upload -> (referrer id, handle, status).

    Naming someone only means something if they actually promoted this deal, so
    the referrer must have claimed it themselves. Their claim does NOT have to be
    approved yet — it may still be sitting in the admin queue, and refusing the
    referee over our own backlog would punish the wrong person. We record which
    of the two it is so the member can be told.

    Advisory: nothing is paid here. The status is re-checked when this claim
    confirms, because a 'pending' claim can still be approved or rejected long
    after this upload.
    """
    normalized = normalize_handle(raw_handle)
    referrer = session.exec(
        select(User).where(func.lower(User.instagram_handle) == normalized)
    ).first()
    if referrer is None:
        raise HTTPException(status_code=422, detail="Referrer handle not found.")
    if referrer.id == user.id:
        raise HTTPException(status_code=422, detail="You can't refer yourself.")
    if campaign_id is None:
        raise HTTPException(
            status_code=422,
            detail="Choose which deal this receipt is for before saying who referred you.")

    # Their claims on THIS deal. A rejected one doesn't count: the admin has
    # already thrown it out, so it's no evidence they promoted anything. Nor
    # does a visit claim — it proves they shopped here, not that they posted
    # anything anyone could have been referred BY.
    claims = session.exec(
        select(Receipt).where(
            Receipt.user_id == referrer.id,
            Receipt.campaign_id == campaign_id,
            Receipt.claim_kind == "post",
            Receipt.status != "rejected",
        )
    ).all()
    if not claims:
        raise HTTPException(
            status_code=422,
            detail=f"@{normalized} hasn't claimed this deal, so they can't have "
                   f"referred you to it.")

    status = ("verified" if any(c.status in APPROVED_STATUSES for c in claims)
              else "pending")
    return referrer.id, raw_handle.strip(), status


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


@router.post("", response_model=ReceiptOut, status_code=201,
             dependencies=[rate_limit("receipts", limit=30, window=3600)])
def create_receipt(
    background: BackgroundTasks,
    post_id: str = Form(""),
    campaign_id: Optional[int] = Form(None),
    referred_by_handle: Optional[str] = Form(None),
    image: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Upload a receipt, either against one of this user's posts or on its own.

    With a `post_id` this is the original kind of claim: one receipt per
    (user, post), and re-uploading for the same post replaces it and resets to
    pending.

    Without one it is a VISIT claim — a repeat trip the member didn't post
    about. It earns whatever the merchant set for that campaign, and there is no
    replacement rule, because two visit claims are two separate visits and
    collapsing them would erase exactly the repeat-custom signal the whole
    feature exists to capture.

    `referred_by_handle` is optional — the Instagram handle of the person whose
    post led this user to buy. Attribution only: no reward is granted here.
    """
    post_id = post_id.strip()
    kind = "post" if post_id else "visit"

    campaign = session.get(Campaign, campaign_id) if campaign_id else None
    if kind == "visit":
        # A visit claim has no post to anchor it, so the deal has to be named —
        # otherwise there is nothing to say whose shop the receipt is from.
        if campaign is None:
            raise HTTPException(
                status_code=422,
                detail="Choose which deal this receipt is for.")
        if not campaign.visits_enabled:
            raise HTTPException(
                status_code=422,
                detail=("This deal doesn't accept repeat visits yet — share a "
                        "post to claim it."))

    referrer_id: Optional[int] = None
    referral_handle, referral_status = "", ""
    if referred_by_handle and referred_by_handle.strip():
        if kind == "visit":
            # Being referred is about how you found the place, which is the
            # first visit. Letting a repeat trip name a referrer would pay a
            # bonus for a customer the merchant already had.
            raise HTTPException(
                status_code=422,
                detail="You can only name who referred you on your first claim "
                       "for a deal, when you post about it.")
        referrer_id, referral_handle, referral_status = _resolve_referral(
            referred_by_handle, campaign_id, user, session)

    try:
        key, digest = upload_receipt(image)
    except StorageTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except StorageUploadError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # The same image file can only ever back one claim. Re-uploading for the
    # SAME post is a replacement (below) and stays allowed; anything else — a
    # second post, a second account — is one purchase claimed twice.
    #
    # Byte-identical only, so there are no false positives, but equally it is
    # beaten by re-saving the photo. Catching that needs the receipt's own
    # order number, which arrives with the extraction step.
    # A visit claim has no post, so it has no replacement case either — every
    # identical image is a duplicate, full stop.
    clash = next(
        (r for r in session.exec(
            select(Receipt).where(Receipt.image_sha256 == digest)).all()
         if not (kind == "post" and r.user_id == user.id
                 and r.post_id == post_id)),
        None,
    )
    if clash is not None:
        delete_receipt(key)             # don't leave the rejected upload behind
        raise HTTPException(
            status_code=409,
            detail="This receipt image has already been submitted with another "
                   "claim. Please upload the receipt for this purchase.")

    # Snapshot the deal's brand + cashback amount so the claim is self-contained.
    # A visit is worth what the merchant set for one, which is its own figure
    # rather than a share of the posted rate.
    brand, amount = "", 0.0
    if campaign is not None:
        brand = campaign.brand
        amount = (campaign.visit_earn if kind == "visit"
                  else _earn_to_amount(campaign.earn))

    # Cashback is NOT confirmed on upload. The claim stays 'pending' and clears
    # automatically 3 days after the post date (see app/cashback.py); admin can
    # reject it within that window.
    #
    # Only a post claim can replace an earlier one. Visit claims all carry
    # post_id = "", so looking one up by (user, post_id) would match the
    # member's LAST visit and overwrite it — turning a customer's repeat trips
    # into a single row, which is the precise opposite of what they are for.
    existing = session.exec(
        select(Receipt).where(Receipt.user_id == user.id,
                              Receipt.post_id == post_id)
    ).first() if kind == "post" else None
    if existing:
        # Drop the photo this one replaces. Without it the old object stays in
        # the private bucket forever with nothing pointing at it — billed, and
        # (worse) missed by the cleanup that deletes a member's receipts when
        # they close their account.
        delete_receipt(existing.image_key)
        existing.image_key = key
        existing.image_sha256 = digest
        existing.campaign_id = campaign_id
        existing.brand = brand
        existing.amount = amount
        existing.status = "pending"
        existing.uploaded_at = datetime.now(timezone.utc)
        existing.referred_by_user_id = referrer_id
        existing.referred_by_handle = referral_handle
        existing.referral_status = referral_status
        receipt = existing
    else:
        receipt = Receipt(
            user_id=user.id, post_id=post_id, claim_kind=kind,
            campaign_id=campaign_id,
            brand=brand, amount=amount, image_key=key, image_sha256=digest,
            status="pending", referred_by_user_id=referrer_id,
            referred_by_handle=referral_handle, referral_status=referral_status,
        )

    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    # Read the receipt in the background — the member shouldn't wait on a
    # vision call to see their claim, and a failed check must not fail the
    # upload. This can now decide the claim's fate itself (auto-approve) if
    # the score clears the threshold — see app/receipt_verification.py.
    if os.environ.get("CIRQLE_RECEIPT_CHECK", "on") != "off":
        background.add_task(run_claude_verification, receipt.id)

    # A visit claim has no post, so its 3-day clearing window runs from the
    # upload instead — which is what clears_at() already falls back to.
    mention = session.get(Mention, post_id) if post_id else None
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
        summary, reasons = summarise(r)
        out.append(AdminReceiptOut(
            id=r.id, userEmail=u.email, userName=f"{u.first_name} {u.last_name}",
            postId=r.post_id, brand=r.brand, amount=r.amount, status=st,
            uploadedAt=r.uploaded_at, imageUrl=receipt_view_url(r.image_key),
            checkStatus=r.check_status, checkScore=r.check_score,
            checkSummary=summary, checkReasons=reasons,
        ))
    return out

_BUCKET_SIZE = 10


@router.post("/{receipt_id}/recheck", response_model=AdminReceiptOut,
             dependencies=[Depends(require_admin)])
def recheck_receipt(receipt_id: int, background: BackgroundTasks,
                    session: Session = Depends(get_session)):
    """Admin: run the automated check on this claim again.

    Checks fail for ordinary reasons — a transient AWS error, an image format
    Textract won't read — and without this there is no way to retry one short
    of re-uploading the receipt. Also useful after the scoring rules change.
    Reads nothing and decides nothing; it only refreshes the advisory columns.
    """
    receipt = session.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    background.add_task(check_receipt, receipt_id)

    user = session.get(User, receipt.user_id)
    summary, reasons = summarise(receipt)
    return AdminReceiptOut(
        id=receipt.id, userEmail=user.email if user else "",
        userName=f"{user.first_name} {user.last_name}" if user else "",
        postId=receipt.post_id, brand=receipt.brand, amount=receipt.amount,
        status=admin_status(receipt, _post_ts_of(receipt, session)),
        uploadedAt=receipt.uploaded_at,
        imageUrl=receipt_view_url(receipt.image_key),
        checkStatus=receipt.check_status, checkScore=receipt.check_score,
        checkSummary=summary, checkReasons=reasons,
    )


@router.post("/{receipt_id}/reverify", response_model=AdminReceiptOut,
             dependencies=[Depends(require_admin)])
def reverify_receipt(receipt_id: int, background: BackgroundTasks,
                     session: Session = Depends(get_session)):
    """Admin: run the Claude-vision verification again.

    The usual reason is that a claim was stuck in pending_admin_review because
    the merchant's reference receipt wasn't ready yet at upload time — once
    they've since uploaded one, this is how the claim gets a fresh chance to
    auto-decide instead of an admin having to review it by hand. Also useful
    after a transient Claude/network failure.
    """
    receipt = session.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    background.add_task(run_claude_verification, receipt_id)

    user = session.get(User, receipt.user_id)
    summary, reasons = summarise(receipt)
    return AdminReceiptOut(
        id=receipt.id, userEmail=user.email if user else "",
        userName=f"{user.first_name} {user.last_name}" if user else "",
        postId=receipt.post_id, brand=receipt.brand, amount=receipt.amount,
        status=admin_status(receipt, _post_ts_of(receipt, session)),
        uploadedAt=receipt.uploaded_at,
        imageUrl=receipt_view_url(receipt.image_key),
        checkStatus=receipt.check_status, checkScore=receipt.check_score,
        checkSummary=summary, checkReasons=reasons,
    )


def _verification_reason(r: Receipt) -> str:
    """Why a receipt landed in the manual-review queue, derived from what's
    already stored rather than a separate reason-code column."""
    if r.verification_error:
        return f"verification failed: {r.verification_error}"
    detail = json.loads(r.authenticity_detail) if r.authenticity_detail else {}
    if detail.get("duplicate_hash_match_receipt_id"):
        return f"duplicate image (matches receipt #{detail['duplicate_hash_match_receipt_id']})"
    if not AUTO_APPROVAL_ENABLED:
        return "auto-approval is currently disabled"
    if r.overall_score is not None and r.overall_score < VERIFICATION_AUTO_APPROVE_THRESHOLD:
        return f"score {r.overall_score:.2f} below the {VERIFICATION_AUTO_APPROVE_THRESHOLD} threshold"
    return "merchant reference receipt not ready"


def _verification_out(r: Receipt, u: Optional[User], reason: str = "") -> AdminVerificationReceiptOut:
    auth_detail = json.loads(r.authenticity_detail) if r.authenticity_detail else {}
    return AdminVerificationReceiptOut(
        id=r.id, userEmail=u.email if u else "",
        userName=f"{u.first_name} {u.last_name}" if u else "",
        postId=r.post_id, brand=r.brand, amount=r.amount,
        verificationStatus=r.verification_status, uploadedAt=r.uploaded_at,
        imageUrl=receipt_view_url(r.image_key),
        authenticityScore=r.authenticity_score,
        authenticityDetail=auth_detail or None,
        purchaseMatchScore=r.purchase_match_score,
        purchaseMatchDetail=json.loads(r.purchase_match_detail) if r.purchase_match_detail else None,
        overallScore=r.overall_score,
        redFlags=auth_detail.get("red_flags", []),
        reason=reason,
    )


@router.get("/admin/manual-review", response_model=list[AdminVerificationReceiptOut],
            dependencies=[Depends(require_admin)])
def manual_review_queue(session: Session = Depends(get_session)):
    """Admin: claims the Claude-vision pipeline did NOT auto-approve — the
    real review queue. Below-threshold score, a duplicate-image hit, a failed
    verification call, or a merchant reference that isn't ready yet."""
    rows = session.exec(
        select(Receipt, User)
        .where(Receipt.user_id == User.id, Receipt.verification_status == "pending_admin_review")
        .order_by(Receipt.uploaded_at.desc())
    ).all()
    return [_verification_out(r, u, _verification_reason(r)) for r, u in rows]


@router.get("/admin/auto-approved", response_model=list[AdminVerificationReceiptOut],
            dependencies=[Depends(require_admin)])
def auto_approved_queue(session: Session = Depends(get_session)):
    """Admin: read-only audit view of claims the pipeline auto-approved,
    sorted by score ascending so the borderline ones surface first. Nothing
    here needs action — it's a spot-check trail, useful while payout rails
    and KYC/AML review are still open items."""
    rows = session.exec(
        select(Receipt, User)
        .where(Receipt.user_id == User.id, Receipt.verification_status == "auto_approved")
        .order_by(Receipt.overall_score.asc())
    ).all()
    return [_verification_out(r, u) for r, u in rows]


@router.get("/admin/calibration", response_model=AdminCheckCalibration,
            dependencies=[Depends(require_admin)])
def check_calibration(session: Session = Depends(get_session)):
    """How the automated check's scores line up with the admin's own decisions.

    This is the evidence for turning on auto-approval. The important output is
    `highestRejectedScore`: the best score that a human still rejected. A safe
    threshold has to sit above it — otherwise switching on auto-approval would
    have waved through a claim the admin had turned down.
    """
    rows = session.exec(
        select(Receipt).where(Receipt.status.in_(APPROVED_STATUSES + ("rejected",)))
    ).all()
    decided = [r for r in rows if r.check_status == "ok"]
    approved = [r for r in decided if r.status in APPROVED_STATUSES]
    rejected = [r for r in decided if r.status == "rejected"]

    buckets = []
    for low in range(0, 100, _BUCKET_SIZE):
        high = low + _BUCKET_SIZE - 1
        in_band = lambda rs: sum(1 for r in rs if low <= r.check_score <= high)
        buckets.append(AdminCheckBucket(label=f"{low}-{high}",
                                        approved=in_band(approved),
                                        rejected=in_band(rejected)))

    out = AdminCheckCalibration(
        decided=len(decided), unchecked=len(rows) - len(decided),
        approved=len(approved), rejected=len(rejected), buckets=buckets,
    )

    if not decided:
        out.verdict = ("No decided claims have been checked yet. Leave the check "
                       "running in the background and come back once you have "
                       "approved and rejected a few dozen.")
        return out

    if not rejected:
        out.verdict = (f"{len(approved)} approved claim(s) checked, none rejected "
                       f"yet. A threshold needs rejections to calibrate against — "
                       f"there is nothing yet to say what a bad claim scores like.")
        return out

    out.highestRejectedScore = max(r.check_score for r in rejected)
    threshold = out.highestRejectedScore + 1
    out.wouldAutoApprove = sum(1 for r in approved if r.check_score >= threshold)
    out.coveragePct = round(100 * out.wouldAutoApprove / len(approved)) if approved else 0

    if threshold > 100:
        out.verdict = ("At least one rejected claim scored 100, so no threshold is "
                       "safe. The scoring rules are missing whatever made you "
                       "reject it — worth looking at that claim before going further.")
    elif out.coveragePct < 20:
        out.suggestedThreshold = threshold
        out.verdict = (f"A safe threshold is {threshold}, but it would only cover "
                       f"{out.coveragePct}% of your approvals — barely worth "
                       f"automating. Approvals and rejections are scoring too "
                       f"similarly for the rules to separate them yet.")
    else:
        out.suggestedThreshold = threshold
        out.verdict = (f"Approving automatically at {threshold} or above would have "
                       f"handled {out.wouldAutoApprove} of {len(approved)} approvals "
                       f"({out.coveragePct}%) without touching anything you rejected.")
    return out


def _post_ts_of(r: Receipt, session: Session) -> Optional[datetime]:
    m = session.get(Mention, r.post_id)
    return parse_post_ts(m.timestamp) if m else None


def _apply_shadow_scoring(r: Receipt, session: Session) -> None:
    """Stamp AQS/payout audit fields and decide the outcome status.

    DISABLED as of 2026-09-10: not called from verify_receipt/bulk_verify_receipts
    right now while the cashback algorithm is being redesigned. Left in place
    (not deleted) along with app/aqs.py and the Campaign/Receipt/Mention schema
    it depends on, so re-enabling is a one-line change once the new plan lands.
    Covered by tests/test_payout.py and tests/test_shadow_scoring_edge_cases.py,
    which call it directly rather than through the disabled endpoints.

    For every campaign (flat or performance) this computes what compute_payout
    WOULD return and stamps it as `shadow_payout` plus the `_at_approval` audit
    fields — real historical data for later comparison, per the spec's Phase 3.
    It never changes `Receipt.amount` (what the member is actually paid) unless
    the campaign has explicitly opted into `cashback_mode == "performance"`,
    and every real campaign is "flat" in this build, so this is inert to real
    payouts today.

    Wrapped so a scoring bug can never block a real approval: any exception
    here just falls back to the plain `"verified"` outcome.

    Neither this endpoint nor bulk-verify guards against being called again on
    a receipt that's already `"verified"` (bulk-verify's own filter is `status
    not in ("pending", "verified")` — so a *verified* receipt is not skipped).
    That's pre-existing, harmless for flat campaigns (nothing here re-mutates
    `amount`), but for a performance-mode campaign a repeat call would
    otherwise re-deduct the budget and overwrite `amount` a second time for a
    receipt the member was already paid on. `already_verified` guards against
    exactly that, while still letting a `pending_budget_review` receipt (not
    yet actually paid) go through the budget check again after a top-up.
    """
    try:
        already_verified = r.status == "verified"
        r.status = "verified"
        if r.campaign_id is None:
            return
        campaign = session.get(Campaign, r.campaign_id)
        if campaign is None:
            return
        user = session.get(User, r.user_id)
        mentions = session.exec(select(Mention).where(Mention.user_id == r.user_id)).all()
        aqs_result = compute_aqs(user, mentions)

        mention = session.get(Mention, r.post_id)
        likes = (mention.likes_count or 0) if mention else 0
        comments = (mention.comments_count or 0) if mention else 0
        payout_result = compute_payout(campaign, aqs_result.score, likes, comments)

        r.aqs_score_at_approval = payout_result.aqs_score
        r.engagement_multiplier_at_approval = payout_result.engagement_multiplier
        r.engagement_snapshot = json.dumps(payout_result.engagement_snapshot)
        r.algorithm_version = ALGORITHM_VERSION
        r.shadow_payout = payout_result.payout

        if campaign.cashback_mode != "performance":
            return  # shadow-only: real amount/status/budget are untouched
        if already_verified:
            return  # already paid once for this campaign — don't re-deduct/repay

        if campaign.budget_remaining is not None and payout_result.payout > campaign.budget_remaining:
            r.status = "pending_budget_review"
        else:
            if campaign.budget_remaining is not None:
                campaign.budget_remaining -= payout_result.payout
                session.add(campaign)
            r.amount = payout_result.payout
    except Exception:
        r.status = "verified"


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
    # _apply_shadow_scoring(r, session) — disabled while the cashback algorithm
    # is being redesigned; see its docstring below. Left in place, not deleted,
    # so it's a one-line change to re-enable once the new plan lands.
    r.status = "verified"
    r.verification_status = "admin_approved"
    r.decision_source = "admin"
    r.decision_at = datetime.utcnow()
    session.add(r)
    session.commit()
    session.refresh(r)
    log_activity(session, "Approved receipt claim", f"{r.brand or 'Cashback'} £{r.amount:.2f}")
    trigger_cashback_calculation(r.id)   # after commit — opens its own session
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
        # _apply_shadow_scoring(r, session) — disabled, see verify_receipt above.
        r.status = "verified"
        r.verification_status = "admin_approved"
        r.decision_source = "admin"
        r.decision_at = datetime.utcnow()
        session.add(r)
        approved.append(r)

    if approved:
        session.commit()
        total = sum(r.amount for r in approved)
        log_activity(session, f"Approved {len(approved)} receipt claims",
                     f"£{total:.2f} released across {len(approved)} claim(s)")
        for r in approved:   # after commit — each opens its own session
            trigger_cashback_calculation(r.id)

    return AdminBulkVerifyOut(approved=len(approved), failed=len(errors), errors=errors[:20])


@router.post("/{receipt_id}/reject", response_model=ReceiptOut,
             dependencies=[Depends(require_admin)])
def reject_receipt(receipt_id: int, session: Session = Depends(get_session)):
    """Admin: reject a claim (no cashback)."""
    r = session.get(Receipt, receipt_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    r.status = "rejected"
    r.verification_status = "admin_rejected"
    r.decision_source = "admin"
    r.decision_at = datetime.utcnow()
    session.add(r)
    # A rejected claim can't have earned anyone a referral bonus. Take back an
    # unspent one; one already withdrawn is left alone, since the money has gone
    # (see referrals.cancel_for_receipt).
    referrals.cancel_for_receipt(r, session, "The claim it was earned on was rejected.")
    session.commit()
    session.refresh(r)
    log_activity(session, "Rejected receipt claim", f"{r.brand or 'Cashback'} £{r.amount:.2f}")
    return _receipt_out(r, "rejected")


# ── Admin: referral oversight ────────────────────────────────────────────────
@router.get("/admin/referrals", response_model=list[AdminReferralOut],
            dependencies=[Depends(require_admin)])
def admin_list_referrals(session: Session = Depends(get_session)):
    """Every referral on the site, newest first — both sides of each one.

    Shows the ones that paid and the ones that didn't, with the reason, so a
    referral can be judged without reconstructing the checks by hand.
    """
    claims = session.exec(
        select(Receipt).where(Receipt.referred_by_user_id.is_not(None))
        .order_by(Receipt.uploaded_at.desc())
    ).all()
    rewards = {(w.receipt_id, w.kind): w for w in session.exec(
        select(ReferralReward)).all()}

    out: list[AdminReferralOut] = []
    for claim in claims:
        campaign = session.get(Campaign, claim.campaign_id) if claim.campaign_id else None
        referrer = session.get(User, claim.referred_by_user_id)
        referee = session.get(User, claim.user_id)
        # Worked out once per claim: the reason is the same for both sides.
        _ok, reason = referrals.check(claim, session)

        for kind, member, other in (("referrer", referrer, referee),
                                    ("referee", referee, referrer)):
            if member is None:
                continue
            reward = rewards.get((claim.id, kind))
            out.append(AdminReferralOut(
                rewardId=reward.id if reward else None,
                receiptId=claim.id,
                kind=kind,
                memberEmail=member.email,
                memberHandle=member.instagram_handle or "",
                otherHandle=(other.instagram_handle if other else "") or "",
                brand=claim.brand,
                dealTitle=(campaign.card_title or campaign.title or campaign.brand)
                          if campaign else "",
                amount=reward.amount if reward else 0.0,
                status=reward.status if reward else "waiting",
                reason="" if reward else reason,
                date=claim.uploaded_at,
            ))
    return out


@router.post("/admin/referrals/{receipt_id}/cancel",
             response_model=list[AdminReferralOut],
             dependencies=[Depends(require_admin)])
def admin_cancel_referral(receipt_id: int, session: Session = Depends(get_session)):
    """Admin: cancel a referral's bonuses, returning the money to the merchant.

    Cancels BOTH sides — a referral that shouldn't have paid shouldn't have paid
    anyone. A bonus already withdrawn is left alone (see referrals.cancel_for_receipt):
    that money has reached a bank, and pretending otherwise would only make the
    books disagree with reality.
    """
    claim = session.get(Receipt, receipt_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Receipt not found.")
    referrals.cancel_for_receipt(claim, session, "Cancelled by an admin.")
    session.commit()
    log_activity(session, "Cancelled a referral bonus",
                 f"{claim.brand or 'Deal'} — claim #{claim.id}")
    return admin_list_referrals(session)
