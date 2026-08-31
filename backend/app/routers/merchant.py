"""Merchant portal endpoints (Phase 6).

Merchants aren't Cirqle users. An admin turns an *approved* MerchantApplication
into a `Merchant` login (POST /merchant, admin-gated) and hands the brand the
generated password. The merchant then signs in here to see real stats for their
deals (the `Campaign` rows linked by `merchant_id`) and to message the admin.

Merchant auth uses a JWT with a typ="merchant" claim (see security.py) so a
merchant token can't reach user endpoints and vice-versa.
"""
import json
import secrets
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlmodel import Session, select

from ..db import get_session
from ..models import (AdminMessageIn, BillingOut, BillingTxnOut, Campaign,
                      CampaignSubmission, CampaignSubmissionIn,
                      CampaignSubmissionOut, DealEvent, DealStat, Mention,
                      Merchant, MerchantApplication, MerchantAuthOut,
                      MerchantCreatedOut, MerchantCreateIn, MerchantMessage,
                      MerchantMessageIn, MerchantMessageOut, MerchantOut,
                      MerchantProfileIn, MerchantProfileOut, MerchantSigninIn,
                      CheckoutSessionOut, MerchantStats, MerchantThreadOut,
                      MerchantTransaction, Receipt, ReferralStat,
                      RejectSubmissionIn, TaggedPostOut, TimePoint, TopUpIn,
                      User)
from ..activity import log_activity
from ..security import (create_merchant_token, get_current_merchant,
                        hash_password, verify_password)
from ..storage import StorageError, delete_image, upload_image
from ..payments import PaymentError, create_topup_session, payments_configured
from .campaigns import require_admin

router = APIRouter(prefix="/merchant", tags=["merchant"])

_TIMESERIES_DAYS = 30
_CASHBACK_GIVEN = ("confirmed", "paid")


def _json_list(raw: str) -> list[str]:
    """Decode a JSON-encoded TEXT list column, tolerating bad/empty data."""
    try:
        val = json.loads(raw or "[]")
        return [str(x) for x in val] if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def _merchant_out(m: Merchant) -> MerchantOut:
    return MerchantOut(
        id=m.id, email=m.email, businessName=m.business_name,
        applicationId=m.application_id, createdAt=m.created_at,
    )


def _message_out(msg: MerchantMessage) -> MerchantMessageOut:
    return MerchantMessageOut(
        id=msg.id, sender=msg.sender, kind=msg.kind,
        body=msg.body, createdAt=msg.created_at,
    )


# ── Merchant auth ──
@router.post("/signin", response_model=MerchantAuthOut)
def signin(data: MerchantSigninIn, session: Session = Depends(get_session)):
    email = data.email.lower()
    m = session.exec(select(Merchant).where(Merchant.email == email)).first()
    if m is None or not verify_password(data.password, m.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    return MerchantAuthOut(token=create_merchant_token(m.id), merchant=_merchant_out(m))


@router.get("/me", response_model=MerchantOut)
def me(merchant: Merchant = Depends(get_current_merchant)):
    return _merchant_out(merchant)


# ── Brand profile (how members see the brand in the app) ──
def _profile_out(m: Merchant) -> MerchantProfileOut:
    return MerchantProfileOut(
        id=m.id, email=m.email, businessName=m.business_name, bio=m.bio,
        categories=_json_list(m.categories), website=m.website,
        instagram=m.instagram, tiktok=m.tiktok, youtube=m.youtube,
        facebook=m.facebook, tips=m.tips, logoUrl=m.logo_url,
        createdAt=m.created_at,
    )


@router.get("/profile", response_model=MerchantProfileOut)
def get_profile(merchant: Merchant = Depends(get_current_merchant)):
    return _profile_out(merchant)


@router.patch("/profile", response_model=MerchantProfileOut)
def update_profile(data: MerchantProfileIn,
                   merchant: Merchant = Depends(get_current_merchant),
                   session: Session = Depends(get_session)):
    """Merchant edits their public brand profile. Only sent fields change."""
    if data.businessName is not None:
        name = data.businessName.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Brand name can't be empty.")
        merchant.business_name = name
    if data.bio is not None:
        merchant.bio = data.bio.strip()
    if data.categories is not None:
        cats = [c.strip() for c in data.categories if c and c.strip()]
        merchant.categories = json.dumps(cats)
    if data.website is not None:
        merchant.website = data.website.strip()
    if data.instagram is not None:
        merchant.instagram = data.instagram.strip().lstrip("@")
    if data.tiktok is not None:
        merchant.tiktok = data.tiktok.strip().lstrip("@")
    if data.youtube is not None:
        merchant.youtube = data.youtube.strip().lstrip("@")
    if data.facebook is not None:
        merchant.facebook = data.facebook.strip().lstrip("@")
    if data.tips is not None:
        merchant.tips = data.tips.strip()
    session.add(merchant)
    session.commit()
    session.refresh(merchant)
    return _profile_out(merchant)


@router.post("/logo", response_model=MerchantProfileOut)
def upload_logo(image: UploadFile = File(...),
                merchant: Merchant = Depends(get_current_merchant),
                session: Session = Depends(get_session)):
    """Upload a brand logo (stored publicly, same as campaign images)."""
    try:
        url = upload_image(image)
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    old_logo = merchant.logo_url
    merchant.logo_url = url
    session.add(merchant)
    session.commit()
    session.refresh(merchant)
    # New logo saved — drop the one it replaced from the bucket.
    if old_logo:
        delete_image(old_logo)
    return _profile_out(merchant)


# ── Posts that tagged this merchant (from shoppers' claims) ──
@router.get("/posts", response_model=list[TaggedPostOut])
def tagged_posts(merchant: Merchant = Depends(get_current_merchant),
                 session: Session = Depends(get_session)):
    """Instagram posts shoppers tagged the brand in — the posts behind the
    receipts claimed against this merchant's deals — with engagement counts
    pulled from the scraped `Mention` rows when available."""
    campaign_ids = [
        c.id for c in session.exec(
            select(Campaign).where(Campaign.merchant_id == merchant.id)).all()
    ]
    if not campaign_ids:
        return []

    receipts = session.exec(
        select(Receipt).where(Receipt.campaign_id.in_(campaign_ids))
        .order_by(Receipt.uploaded_at.desc())
    ).all()

    out: list[TaggedPostOut] = []
    seen: set[str] = set()
    for r in receipts:
        if not r.post_id or r.post_id in seen:
            continue
        seen.add(r.post_id)
        m = session.get(Mention, r.post_id)     # the scraped post, if we have it
        out.append(TaggedPostOut(
            postId=r.post_id,
            imageUrl=(m.display_url if m else None),
            caption=(m.caption if m and m.caption else ""),
            ownerUsername=(m.owner_username if m and m.owner_username else ""),
            likes=(m.likes_count if m else None),
            comments=(m.comments_count if m else None),
            views=None,                          # IG photo posts have no view count
            url=(m.url if m else None),
            dealTitle=r.brand,
            date=(m.timestamp if m and m.timestamp else r.uploaded_at.isoformat()),
        ))
    return out


# ── Referral attribution: which members' posts drove other members' claims ──
@router.get("/referrals", response_model=list[ReferralStat])
def referrals(merchant: Merchant = Depends(get_current_merchant),
             session: Session = Depends(get_session)):
    """Claims referred by another member, grouped by referrer + campaign —
    the "top referring posts" view. Attribution only: `referredCashback` is
    the referred claims' own cashback value, not a reward paid to the
    referrer — no reward scheme exists yet (see Receipt.referred_by_user_id)."""
    campaigns = session.exec(
        select(Campaign).where(Campaign.merchant_id == merchant.id)).all()
    campaign_ids = [c.id for c in campaigns]
    if not campaign_ids:
        return []
    campaign_by_id = {c.id: c for c in campaigns}

    receipts = session.exec(
        select(Receipt).where(
            Receipt.campaign_id.in_(campaign_ids),
            Receipt.referred_by_user_id.is_not(None),
        )
    ).all()

    groups: dict[tuple[int, int], list[Receipt]] = {}
    for r in receipts:
        groups.setdefault((r.referred_by_user_id, r.campaign_id), []).append(r)

    out: list[ReferralStat] = []
    for (referrer_id, campaign_id), group in groups.items():
        referrer = session.get(User, referrer_id)
        if referrer is None:
            continue
        camp = campaign_by_id[campaign_id]
        # The referrer's own claim on this campaign is the post that started
        # the chain — "the referrer's Mention for the originating post".
        origin = session.exec(
            select(Receipt).where(
                Receipt.user_id == referrer_id,
                Receipt.campaign_id == campaign_id,
            )
        ).first()
        mention = session.get(Mention, origin.post_id) if origin else None
        out.append(ReferralStat(
            referrerUserId=referrer_id,
            referrerHandle=referrer.instagram_handle or "",
            campaignId=campaign_id,
            brand=camp.brand,
            dealTitle=camp.card_title or camp.title or camp.brand,
            postId=(origin.post_id if origin else None),
            imageUrl=(mention.display_url if mention else None),
            claims=len(group),
            referredCashback=round(
                sum(r.amount for r in group if r.status in _CASHBACK_GIVEN), 2),
        ))
    out.sort(key=lambda s: s.claims, reverse=True)
    return out


# ── Billing: prepaid balance that funds shopper cashback ──
def _billing(merchant: Merchant, session: Session) -> BillingOut:
    topups = session.exec(
        select(MerchantTransaction)
        .where(MerchantTransaction.merchant_id == merchant.id)
    ).all()
    campaign_ids = [
        c.id for c in session.exec(
            select(Campaign).where(Campaign.merchant_id == merchant.id)).all()
    ]
    receipts = session.exec(
        select(Receipt).where(Receipt.campaign_id.in_(campaign_ids))
    ).all() if campaign_ids else []

    total_topped = round(sum(t.amount for t in topups), 2)
    given = [r for r in receipts if r.status in _CASHBACK_GIVEN]
    cashback_given = round(sum(r.amount for r in given), 2)
    pending = round(sum(r.amount for r in receipts if r.status == "pending"), 2)
    balance = round(total_topped - cashback_given, 2)

    txns = [BillingTxnOut(kind="topup", amount=round(t.amount, 2),
                          description=t.description or "Account top-up",
                          date=t.created_at) for t in topups]
    txns += [BillingTxnOut(kind="cashback", amount=-round(r.amount, 2),
                           description=f"Cashback — {r.brand}", date=r.uploaded_at)
             for r in given]
    txns.sort(key=lambda t: t.date, reverse=True)

    return BillingOut(
        balance=balance, totalToppedUp=total_topped, cashbackGiven=cashback_given,
        pendingCashback=pending, transactions=txns,
    )


@router.get("/billing", response_model=BillingOut)
def billing(merchant: Merchant = Depends(get_current_merchant),
            session: Session = Depends(get_session)):
    return _billing(merchant, session)


@router.post("/billing/checkout", response_model=CheckoutSessionOut)
def billing_checkout(data: TopUpIn, request: Request,
                     merchant: Merchant = Depends(get_current_merchant)):
    """Start a Stripe Checkout for a prepaid top-up and return the hosted URL.

    The balance is credited by the Stripe webhook after the payment succeeds
    (see routers/stripe_webhook.py), never here — a redirect can be faked.
    """
    if not payments_configured():
        raise HTTPException(status_code=503, detail="Card payments aren't set up yet.")
    amount = round(float(data.amount), 2)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Enter an amount above £0.")
    if amount > 100000:
        raise HTTPException(status_code=422, detail="That top-up is too large.")
    try:
        url = create_topup_session(merchant, amount, request.headers.get("origin", ""))
    except PaymentError:
        raise HTTPException(status_code=502, detail="Could not start checkout. Please try again.")
    return CheckoutSessionOut(url=url)


# ── Merchant dashboard stats ──
def _compute_stats(merchant: Merchant, session: Session) -> MerchantStats:
    campaigns = session.exec(
        select(Campaign).where(Campaign.merchant_id == merchant.id)
    ).all()
    campaign_ids = [c.id for c in campaigns]

    if not campaign_ids:
        return MerchantStats(
            dealsCount=0, views=0, clicks=0, claims=0, cashbackGiven=0.0,
            pendingCashback=0.0, conversion=0.0,
            timeseries=_empty_timeseries(), deals=[],
        )

    events = session.exec(
        select(DealEvent).where(DealEvent.campaign_id.in_(campaign_ids))
    ).all()
    receipts = session.exec(
        select(Receipt).where(Receipt.campaign_id.in_(campaign_ids))
    ).all()

    views = sum(1 for e in events if e.kind == "view")
    clicks = sum(1 for e in events if e.kind == "click")
    claims = len(receipts)
    cashback_given = round(
        sum(r.amount for r in receipts if r.status in _CASHBACK_GIVEN), 2)
    pending_cashback = round(
        sum(r.amount for r in receipts if r.status == "pending"), 2)
    conversion = round(claims / views * 100, 1) if views else 0.0

    # ── Per-deal breakdown ──
    deals = []
    for c in campaigns:
        c_events = [e for e in events if e.campaign_id == c.id]
        c_receipts = [r for r in receipts if r.campaign_id == c.id]
        deals.append(DealStat(
            campaignId=c.id, brand=c.brand, title=c.card_title or c.title or c.brand,
            views=sum(1 for e in c_events if e.kind == "view"),
            clicks=sum(1 for e in c_events if e.kind == "click"),
            claims=len(c_receipts),
            cashback=round(sum(r.amount for r in c_receipts
                               if r.status in _CASHBACK_GIVEN), 2),
        ))

    return MerchantStats(
        dealsCount=len(campaigns), views=views, clicks=clicks, claims=claims,
        cashbackGiven=cashback_given, pendingCashback=pending_cashback,
        conversion=conversion,
        timeseries=_build_timeseries(events, receipts), deals=deals,
    )


def _empty_timeseries() -> list[TimePoint]:
    today = date.today()
    return [
        TimePoint(date=str(today - timedelta(days=n)), views=0, clicks=0, claims=0)
        for n in range(_TIMESERIES_DAYS - 1, -1, -1)
    ]


def _build_timeseries(events, receipts) -> list[TimePoint]:
    """Last 30 days of views/clicks/claims, one point per day (zero-filled)."""
    today = date.today()
    start = today - timedelta(days=_TIMESERIES_DAYS - 1)
    buckets = {str(start + timedelta(days=n)): {"views": 0, "clicks": 0, "claims": 0}
               for n in range(_TIMESERIES_DAYS)}

    for e in events:
        key = str(e.created_at.date())
        if key in buckets and e.kind in ("view", "click"):
            buckets[key][e.kind + "s"] += 1
    for r in receipts:
        key = str(r.uploaded_at.date())
        if key in buckets:
            buckets[key]["claims"] += 1

    return [TimePoint(date=d, **buckets[d]) for d in sorted(buckets)]


@router.get("/stats", response_model=MerchantStats)
def stats(merchant: Merchant = Depends(get_current_merchant),
          session: Session = Depends(get_session)):
    return _compute_stats(merchant, session)


# ── Merchant <-> admin messages ──
@router.get("/messages", response_model=list[MerchantMessageOut])
def list_messages(merchant: Merchant = Depends(get_current_merchant),
                  session: Session = Depends(get_session)):
    """This merchant's thread. Marks admin replies as read."""
    msgs = session.exec(
        select(MerchantMessage)
        .where(MerchantMessage.merchant_id == merchant.id)
        .order_by(MerchantMessage.created_at)
    ).all()
    for msg in msgs:
        if msg.sender == "admin" and not msg.read_by_merchant:
            msg.read_by_merchant = True
            session.add(msg)
    session.commit()
    return [_message_out(m) for m in msgs]


@router.post("/messages", response_model=MerchantMessageOut, status_code=201)
def send_message(data: MerchantMessageIn,
                 merchant: Merchant = Depends(get_current_merchant),
                 session: Session = Depends(get_session)):
    """Merchant sends a message or a `deal_request` to the admin."""
    body = data.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Message can't be empty.")
    kind = data.kind if data.kind in ("message", "deal_request") else "message"
    msg = MerchantMessage(
        merchant_id=merchant.id, sender="merchant", kind=kind, body=body,
        read_by_admin=False, read_by_merchant=True,
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return _message_out(msg)


# ── Merchant-submitted campaigns (propose a deal -> admin approves/rejects) ──
def _submission_out(sub: CampaignSubmission) -> CampaignSubmissionOut:
    return CampaignSubmissionOut(
        id=sub.id, brand=sub.brand, cardTitle=sub.card_title, cardDesc=sub.card_desc,
        longDesc=sub.long_desc, category=sub.category, rate=sub.rate, earn=sub.earn,
        spendDesc=sub.spend_desc, expiry=sub.expiry, location=sub.location,
        brandUrl=sub.brand_url, terms=sub.terms, status=sub.status,
        rejectionReason=sub.rejection_reason, campaignId=sub.campaign_id,
        createdAt=sub.created_at,
    )


@router.post("/campaigns", response_model=CampaignSubmissionOut, status_code=201)
def submit_campaign(data: CampaignSubmissionIn,
                    merchant: Merchant = Depends(get_current_merchant),
                    session: Session = Depends(get_session)):
    """Merchant: propose a new deal. Stored as 'pending' for admin review."""
    title = data.cardTitle.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Give your deal a title.")
    sub = CampaignSubmission(
        merchant_id=merchant.id, brand=merchant.business_name,
        card_title=title, card_desc=data.cardDesc.strip(),
        long_desc=data.longDesc.strip(), category=data.category.strip(),
        rate=data.rate, earn=data.earn.strip(), spend_desc=data.spendDesc.strip(),
        expiry=data.expiry.strip(), location="Online · UK",
        brand_url=data.brandUrl.strip(), terms=data.terms.strip(),
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)
    return _submission_out(sub)


@router.get("/campaigns", response_model=list[CampaignSubmissionOut])
def list_my_submissions(merchant: Merchant = Depends(get_current_merchant),
                        session: Session = Depends(get_session)):
    """Merchant: their own submissions (newest first), with status + any reason."""
    subs = session.exec(
        select(CampaignSubmission)
        .where(CampaignSubmission.merchant_id == merchant.id)
        .order_by(CampaignSubmission.id.desc())
    ).all()
    return [_submission_out(s) for s in subs]


@router.get("/campaigns/admin", response_model=list[CampaignSubmissionOut],
            dependencies=[Depends(require_admin)])
def admin_list_submissions(status: str = "", session: Session = Depends(get_session)):
    """Admin: all merchant campaign submissions, newest first. Optional ?status=."""
    stmt = select(CampaignSubmission).order_by(CampaignSubmission.id.desc())
    if status:
        stmt = stmt.where(CampaignSubmission.status == status)
    return [_submission_out(s) for s in session.exec(stmt).all()]


def _campaign_from_submission(sub: CampaignSubmission) -> Campaign:
    """Build a live deal from an approved submission, attributed to the merchant."""
    return Campaign(
        brand=sub.brand,
        title=sub.card_title or sub.brand,
        card_title=sub.card_title or sub.brand,
        card_desc=sub.card_desc,
        long_desc=sub.long_desc,
        emoji="🛍️",
        category=sub.category,
        rate=sub.rate,
        earn=sub.earn,
        spend_desc=sub.spend_desc,
        expiry=sub.expiry or "Ongoing",
        location=sub.location or "Online · UK",
        brand_url=sub.brand_url,
        terms=sub.terms,
        merchant_id=sub.merchant_id,
    )


@router.post("/campaigns/{sub_id}/approve", response_model=CampaignSubmissionOut,
             dependencies=[Depends(require_admin)])
def approve_submission(sub_id: int, session: Session = Depends(get_session)):
    """Admin: approve -> publish a live deal built from the submission."""
    sub = session.get(CampaignSubmission, sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Submission not found.")
    if sub.status == "approved":
        raise HTTPException(status_code=400, detail="Already approved.")

    c = _campaign_from_submission(sub)
    session.add(c)
    session.commit()
    session.refresh(c)

    sub.status = "approved"
    sub.campaign_id = c.id
    sub.rejection_reason = ""
    sub.reviewed_at = datetime.utcnow()
    session.add(sub)
    session.commit()
    session.refresh(sub)
    log_activity(session, "Approved campaign submission", f"{sub.brand} — {sub.card_title} → deal #{c.id}")
    return _submission_out(sub)


@router.post("/campaigns/{sub_id}/reject", response_model=CampaignSubmissionOut,
             dependencies=[Depends(require_admin)])
def reject_submission(sub_id: int, data: RejectSubmissionIn,
                      session: Session = Depends(get_session)):
    """Admin: reject with a reason the merchant will see in their portal."""
    sub = session.get(CampaignSubmission, sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Submission not found.")
    reason = data.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="Give a reason for the rejection.")
    sub.status = "rejected"
    sub.rejection_reason = reason
    sub.reviewed_at = datetime.utcnow()
    session.add(sub)
    session.commit()
    session.refresh(sub)
    log_activity(session, "Rejected campaign submission", f"{sub.brand} — {sub.card_title}")
    return _submission_out(sub)


# ── Admin: create merchant logins + manage message threads ──
@router.post("", response_model=MerchantCreatedOut, status_code=201,
             dependencies=[Depends(require_admin)])
def create_merchant(data: MerchantCreateIn, session: Session = Depends(get_session)):
    """Admin: turn an approved application into a merchant login.

    Generates a one-time password (shown once, only the hash is stored) and
    links the application's published deal to the new merchant so stats attribute.
    """
    app = session.get(MerchantApplication, data.applicationId)
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found.")
    if app.status != "approved":
        raise HTTPException(status_code=400,
                            detail="Approve the application before creating a login.")

    email = (app.email or "").lower().strip()
    if not email:
        raise HTTPException(status_code=400, detail="Application has no email.")
    if session.exec(select(Merchant).where(Merchant.email == email)).first():
        raise HTTPException(status_code=409,
                            detail="A merchant login already exists for this email.")

    password = secrets.token_urlsafe(9)
    merchant = Merchant(
        application_id=app.id, email=email,
        password_hash=hash_password(password), business_name=app.brand,
    )
    session.add(merchant)
    session.commit()
    session.refresh(merchant)

    # Attribute the application's published deal to this merchant (for stats).
    if app.campaign_id is not None:
        camp = session.get(Campaign, app.campaign_id)
        if camp is not None:
            camp.merchant_id = merchant.id
            session.add(camp)
            session.commit()

    log_activity(session, "Created merchant login", merchant.business_name)
    return MerchantCreatedOut(merchant=_merchant_out(merchant), password=password)


@router.get("/messages/admin", response_model=list[MerchantThreadOut],
            dependencies=[Depends(require_admin)])
def admin_list_threads(session: Session = Depends(get_session)):
    """Admin: every merchant's thread, most-unread first."""
    merchants = session.exec(select(Merchant)).all()
    threads = []
    for m in merchants:
        msgs = session.exec(
            select(MerchantMessage)
            .where(MerchantMessage.merchant_id == m.id)
            .order_by(MerchantMessage.created_at)
        ).all()
        unread = sum(1 for msg in msgs
                     if msg.sender == "merchant" and not msg.read_by_admin)
        threads.append(MerchantThreadOut(
            merchantId=m.id, businessName=m.business_name, email=m.email,
            unread=unread, messages=[_message_out(msg) for msg in msgs],
        ))
    threads.sort(key=lambda t: t.unread, reverse=True)
    return threads


@router.post("/messages/admin", response_model=MerchantMessageOut, status_code=201,
             dependencies=[Depends(require_admin)])
def admin_reply(data: AdminMessageIn, session: Session = Depends(get_session)):
    """Admin: reply to a merchant. Marks that merchant's inbound as read."""
    merchant = session.get(Merchant, data.merchantId)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found.")
    body = data.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Message can't be empty.")

    inbound = session.exec(
        select(MerchantMessage).where(
            MerchantMessage.merchant_id == merchant.id,
            MerchantMessage.sender == "merchant",
            MerchantMessage.read_by_admin == False,  # noqa: E712
        )
    ).all()
    for msg in inbound:
        msg.read_by_admin = True
        session.add(msg)

    reply = MerchantMessage(
        merchant_id=merchant.id, sender="admin", kind="message", body=body,
        read_by_admin=True, read_by_merchant=False,
    )
    session.add(reply)
    session.commit()
    session.refresh(reply)
    log_activity(session, "Replied to merchant", merchant.business_name)
    return _message_out(reply)
