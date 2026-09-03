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

from .. import mailer, passwords, tokens
from ..db import get_session
from ..models import (AdminMessageIn, BillingOut, BillingTxnOut, Campaign,
                      CampaignSubmission, CampaignSubmissionIn,
                      CampaignSubmissionOut, DealEvent, DealStat, EmailIn,
                      Mention, MessageOut, ResetPasswordIn,
                      Merchant, MerchantApplication, MerchantAuthOut,
                      MerchantCreatedOut, MerchantCreateIn, MerchantMessage,
                      MerchantMessageIn, MerchantMessageOut, MerchantOut,
                      MerchantProfileIn, MerchantProfileOut, MerchantSigninIn,
                      CheckoutSessionOut, MerchantStats, MerchantThreadOut,
                      PlanOut, RefundIn, SubscribeIn, SubscriptionOut,
                      TopUpQuote,
                      DealReferralsIn, MerchantTransaction, Receipt,
                      ReferralReward, ReferralStat, RejectSubmissionIn,
                      TaggedPostOut, TimePoint, TopUpIn, User, WalletOut)
from ..activity import log_activity
from ..ratelimit import rate_limit
from ..security import (create_merchant_token, get_current_merchant,
                        hash_password, verify_password)
from ..storage import StorageError, delete_image, upload_image
from ..payments import (OVERAGE_RATE, TIERS, PaymentError,
                        create_portal_session, create_subscription_session,
                        create_topup_session, ensure_customer, month_start,
                        payments_configured, quote_topup, refund_topup)
from .auth import SENT_MESSAGE
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
    if m is None:
        # Keep the timing the same as a real account — see auth.py signin.
        hash_password(data.password)
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    if m.must_set_password:
        raise HTTPException(
            status_code=403,
            detail=("Check your email for the invite link and set a password to "
                    "get started."))
    if not verify_password(data.password, m.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    if m.email_verified_at is None:
        raise HTTPException(
            status_code=403,
            detail="Please confirm your email address first — check your inbox.")
    return MerchantAuthOut(token=create_merchant_token(m), merchant=_merchant_out(m))


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
        # Only claims that actually earned cashback count. A rejected or still-
        # clearing claim isn't a referral the merchant got anything from, and
        # counting it here while excluding it from `referredCashback` below made
        # the two figures disagree.
        credited = [r for r in group if r.status in _CASHBACK_GIVEN]
        if not credited:
            continue
        # The referrer's own claim on this campaign is the post that started
        # the chain — "the referrer's Mention for the originating post". Oldest
        # first: with several posts on one deal, the chain starts at the first.
        origin = session.exec(
            select(Receipt).where(
                Receipt.user_id == referrer_id,
                Receipt.campaign_id == campaign_id,
            ).order_by(Receipt.uploaded_at)
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
            claims=len(credited),
            referredCashback=round(sum(r.amount for r in credited), 2),
        ))
    out.sort(key=lambda s: s.claims, reverse=True)
    return out


@router.patch("/deals/{campaign_id}/referrals", response_model=DealStat)
def set_deal_referrals(campaign_id: int, data: DealReferralsIn,
                       merchant: Merchant = Depends(get_current_merchant),
                       session: Session = Depends(get_session)):
    """Turn referral bonuses on or off for one of this merchant's deals.

    Off by default, so funding the referral wallet never silently enrols a deal
    the brand didn't choose. Switching it off stops future bonuses; ones already
    earned are money the member has, and are left alone.
    """
    campaign = session.get(Campaign, campaign_id)
    if campaign is None or campaign.merchant_id != merchant.id:
        raise HTTPException(status_code=404, detail="No such deal.")
    campaign.referrals_enabled = bool(data.enabled)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    stats = _compute_stats(merchant, session)
    return next(d for d in stats.deals if d.campaignId == campaign_id)


# ── Billing: prepaid balance that funds shopper cashback ──
def _month_topups(merchant_id: int, session: Session) -> float:
    """£ of credit topped up since the 1st — what the allowance is measured against.

    Both wallets count towards the one allowance. The allowance is about how much
    money moves through the account, so keeping them separate would let a merchant
    dodge the fee by routing credit through the referral wallet.
    """
    rows = session.exec(
        select(MerchantTransaction).where(
            MerchantTransaction.merchant_id == merchant_id,
            MerchantTransaction.kind == "topup",
            MerchantTransaction.created_at >= month_start(),
        )
    ).all()
    return round(sum(t.amount for t in rows), 2)


def _subscription_out(merchant: Merchant, session: Session) -> SubscriptionOut:
    """The merchant's plan state, including this month's allowance usage."""
    tier = TIERS.get(merchant.tier or "")
    if not tier:
        # No tier: either never subscribed ("none") or cancelled — keep the
        # real status so the portal can say which.
        return SubscriptionOut(status=merchant.subscription_status or "none")
    used = _month_topups(merchant.id, session)
    allowance = tier["allowance"]
    return SubscriptionOut(
        tier=merchant.tier,
        planName=tier["name"],
        status=merchant.subscription_status,
        fee=tier["fee"],
        allowance=allowance,
        allowanceUsed=used,
        allowanceLeft=round(max(0.0, allowance - used), 2),
        renewsAt=merchant.current_period_end,
        canTopUp=merchant.subscription_status == "active",
    )


def _billing(merchant: Merchant, session: Session) -> BillingOut:
    txn_rows = session.exec(
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

    # Only credit rows move a balance; fees are Cirqle's revenue, not credit.
    # Each wallet is funded and spent separately — a full cashback balance pays
    # for no referral bonuses, and vice versa.
    def credit(wallet: str) -> tuple[float, float]:
        rows = [t for t in txn_rows
                if t.kind in ("topup", "refund") and t.wallet == wallet]
        return (round(sum(t.amount for t in rows if t.kind == "topup"), 2),
                round(sum(t.amount for t in rows), 2))

    cash_topped, cash_credit = credit("cashback")
    ref_topped, ref_credit = credit("referral")

    # Each fee has its own row; `fee` on a top-up row is context for that
    # top-up, so counting both would double it.
    fees_paid = round(sum(t.amount for t in txn_rows
                          if t.kind in ("platform_fee", "subscription")), 2)

    given = [r for r in receipts if r.status in _CASHBACK_GIVEN]
    cashback_given = round(sum(r.amount for r in given), 2)
    pending = round(sum(r.amount for r in receipts if r.status == "pending"), 2)
    balance = round(cash_credit - cashback_given, 2)

    rewards = session.exec(
        select(ReferralReward).where(
            ReferralReward.merchant_id == merchant.id,
            ReferralReward.status.in_(("available", "paid")),
        )
    ).all()
    referral_paid = round(sum(r.amount for r in rewards), 2)

    _LABELS = {"topup": "Account top-up", "platform_fee": "Platform fee",
               "subscription": "Membership fee", "refund": "Refund to card"}
    txns = [BillingTxnOut(kind=t.kind, amount=round(t.amount, 2),
                          description=t.description or _LABELS.get(t.kind, "Charge"),
                          date=t.created_at, wallet=t.wallet) for t in txn_rows]
    txns += [BillingTxnOut(kind="cashback", amount=-round(r.amount, 2),
                           description=f"Cashback — {r.brand}", date=r.uploaded_at,
                           wallet="cashback")
             for r in given]
    txns += [BillingTxnOut(kind="referral", amount=-round(w.amount, 2),
                           description="Referral bonus", date=w.created_at,
                           wallet="referral")
             for w in rewards]
    txns.sort(key=lambda t: t.date, reverse=True)

    return BillingOut(
        balance=balance, totalToppedUp=cash_topped, cashbackGiven=cashback_given,
        pendingCashback=pending, feesPaid=fees_paid,
        wallets=[
            WalletOut(wallet="cashback", balance=balance, toppedUp=cash_topped,
                      spent=cashback_given, pending=pending),
            WalletOut(wallet="referral", balance=round(ref_credit - referral_paid, 2),
                      toppedUp=ref_topped, spent=referral_paid, pending=0.0),
        ],
        subscription=_subscription_out(merchant, session), transactions=txns,
    )


@router.get("/billing", response_model=BillingOut)
def billing(merchant: Merchant = Depends(get_current_merchant),
            session: Session = Depends(get_session)):
    return _billing(merchant, session)


# ── Plans, subscriptions and the card on file ─────────────────────────────────
@router.get("/plans", response_model=list[PlanOut])
def plans():
    """The membership tiers. Public — the plans grid on contact.html reads this,
    so the marketing page and the billing portal can never drift apart."""
    return [PlanOut(id=key, name=t["name"], fee=t["fee"], allowance=t["allowance"],
                    feeRate=OVERAGE_RATE, blurb=t["blurb"])
            for key, t in TIERS.items()]


def _require_payments():
    if not payments_configured():
        raise HTTPException(status_code=503, detail="Card payments aren't set up yet.")


@router.post("/billing/subscribe", response_model=CheckoutSessionOut)
def subscribe(data: SubscribeIn, request: Request,
              merchant: Merchant = Depends(get_current_merchant),
              session: Session = Depends(get_session)):
    """Start (or switch to) a membership plan.

    Stripe Checkout collects the card and billing address and starts the
    subscription in one hosted step — this is where a brand's card first
    reaches us, right after the admin approves them.
    """
    _require_payments()
    if data.tier not in TIERS:
        raise HTTPException(status_code=422, detail="Unknown plan.")
    try:
        customer_id = ensure_customer(merchant, session)
        url = create_subscription_session(merchant, data.tier, customer_id,
                                          request.headers.get("origin", ""))
    except PaymentError:
        raise HTTPException(status_code=502, detail="Could not start checkout. Please try again.")
    return CheckoutSessionOut(url=url)


@router.post("/billing/portal", response_model=CheckoutSessionOut)
def billing_portal(request: Request,
                   merchant: Merchant = Depends(get_current_merchant),
                   session: Session = Depends(get_session)):
    """Open Stripe's Billing Portal — update the card, change the billing
    address, download invoices, switch or cancel the plan."""
    _require_payments()
    if not merchant.stripe_customer_id:
        raise HTTPException(status_code=409, detail="Choose a plan first.")
    try:
        url = create_portal_session(merchant.stripe_customer_id,
                                    request.headers.get("origin", ""))
    except PaymentError:
        raise HTTPException(status_code=502, detail="Could not open the billing portal.")
    return CheckoutSessionOut(url=url)


# ── Prepaid top-ups ───────────────────────────────────────────────────────────
def _validate_topup(merchant: Merchant, amount: float) -> float:
    """Shared guard for the quote and the checkout, so a hand-crafted request
    can't skip the plan gate that the UI enforces."""
    if merchant.subscription_status != "active":
        raise HTTPException(
            status_code=409,
            detail="Choose a membership plan before adding credit.")
    credit = round(float(amount), 2)
    if credit <= 0:
        raise HTTPException(status_code=422, detail="Enter an amount above £0.")
    if credit > 100000:
        raise HTTPException(status_code=422, detail="That top-up is too large.")
    return credit


def _validate_wallet(wallet: str) -> str:
    """Which pot the money is for. Rejected rather than defaulted: silently
    crediting the wrong wallet would be a real accounting error."""
    if wallet not in ("cashback", "referral"):
        raise HTTPException(status_code=422, detail="Unknown wallet.")
    return wallet


@router.get("/billing/quote", response_model=TopUpQuote)
def topup_quote(amount: float, wallet: str = "cashback",
                merchant: Merchant = Depends(get_current_merchant),
                session: Session = Depends(get_session)):
    """What a top-up of `amount` will cost — so the merchant sees the platform
    fee before they commit, never as a surprise on the Stripe page."""
    _validate_wallet(wallet)
    credit = _validate_topup(merchant, amount)
    return TopUpQuote(**quote_topup(merchant, credit,
                                    _month_topups(merchant.id, session)))


@router.post("/billing/checkout", response_model=CheckoutSessionOut)
def billing_checkout(data: TopUpIn, request: Request,
                     merchant: Merchant = Depends(get_current_merchant),
                     session: Session = Depends(get_session)):
    """Start a Stripe Checkout for a prepaid top-up and return the hosted URL.

    The fee is recomputed here rather than trusted from the browser, and the
    balance is credited by the webhook, never on the redirect.
    """
    _require_payments()
    wallet = _validate_wallet(data.wallet)
    credit = _validate_topup(merchant, data.amount)
    quote = quote_topup(merchant, credit, _month_topups(merchant.id, session))
    try:
        customer_id = ensure_customer(merchant, session)
        url = create_topup_session(merchant, quote, customer_id,
                                   request.headers.get("origin", ""), wallet)
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
    rewards = session.exec(
        select(ReferralReward).where(
            ReferralReward.campaign_id.in_(campaign_ids),
            ReferralReward.status.in_(("available", "paid")),
        )
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
            referralsEnabled=c.referrals_enabled,
            referralsPaid=round(sum(w.amount for w in rewards
                                    if w.campaign_id == c.id), 2),
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
    """Merchant: propose a new deal. Stored as 'pending' for admin review.

    Gated on an active membership — a lapsed or unpaid account keeps read
    access to its stats but can't add new deals until billing is sorted.
    """
    if merchant.subscription_status != "active":
        raise HTTPException(
            status_code=409,
            detail="An active membership plan is needed to submit new deals.")
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

    No password is generated. The brand gets an emailed invite link and chooses
    their own password, which also proves they own the address — so a working
    password never has to be relayed by hand over chat or email.
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

    merchant = Merchant(
        application_id=app.id, email=email,
        # Unusable until they set their own via the invite link. Random rather
        # than blank so no crafted input can ever match it.
        password_hash=hash_password(secrets.token_urlsafe(32)),
        business_name=app.brand, must_set_password=True,
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

    raw = tokens.issue(session, "invite", "merchant", merchant.id)
    delivered = mailer.send_merchant_invite(merchant.email, merchant.business_name, raw)

    log_activity(session, "Created merchant login", merchant.business_name)
    return MerchantCreatedOut(
        merchant=_merchant_out(merchant),
        inviteSent=delivered,
        message=(f"Invite emailed to {merchant.email}. They set their own "
                 f"password from the link; it expires in 7 days."),
    )


# ── Setting and resetting a merchant password ────────────────────────────────
# Mirrors the member flow in auth.py: generic answers so the endpoints can't be
# used to find out which brands have accounts, single-use links, and a password
# change that signs every other device out.

@router.post("/set-password", response_model=MessageOut,
             dependencies=[rate_limit("reset", limit=10, window=3600)])
def set_password(data: ResetPasswordIn, session: Session = Depends(get_session)):
    """Redeem an invite link: choose a password and confirm the address.

    Reaching the inbox is what proves the address belongs to the brand, so this
    completes verification as well as setting the password.
    """
    token = tokens.redeem(session, data.token, "invite")
    if token is None or token.subject_type != "merchant":
        raise HTTPException(
            status_code=400,
            detail="This link is invalid or has expired. Ask us for a new one.")
    merchant = session.get(Merchant, token.subject_id)
    if merchant is None:
        raise HTTPException(status_code=400, detail="This link is no longer valid.")
    passwords.validate(data.newPassword, merchant.email)

    merchant.password_hash = hash_password(data.newPassword)
    merchant.password_changed_at = datetime.utcnow()
    merchant.email_verified_at = datetime.utcnow()
    merchant.must_set_password = False
    session.add(merchant)
    session.commit()
    return MessageOut(message="Password set. You can now sign in to the portal.")


@router.post("/forgot-password", response_model=MessageOut,
             dependencies=[rate_limit("forgot", limit=5, window=3600)])
def forgot_password(data: EmailIn, session: Session = Depends(get_session)):
    """Email a reset link. Always answers the same way — see auth.py."""
    m = session.exec(select(Merchant).where(Merchant.email == data.email.lower())).first()
    if m is not None:
        # An account still on its invite gets the invite resent instead: it
        # lasts longer, and "reset" makes no sense before a password exists.
        if m.must_set_password:
            raw = tokens.issue(session, "invite", "merchant", m.id)
            mailer.send_merchant_invite(m.email, m.business_name, raw)
        else:
            raw = tokens.issue(session, "reset_password", "merchant", m.id)
            mailer.send_merchant_reset(m.email, m.business_name, raw)
    return MessageOut(message=SENT_MESSAGE)


@router.post("/reset-password", response_model=MessageOut,
             dependencies=[rate_limit("reset", limit=10, window=3600)])
def reset_password(data: ResetPasswordIn, session: Session = Depends(get_session)):
    """Set a new password from a reset link, signing every device out."""
    token = tokens.redeem(session, data.token, "reset_password")
    if token is None or token.subject_type != "merchant":
        raise HTTPException(
            status_code=400,
            detail="This link is invalid or has expired. Request a new one.")
    merchant = session.get(Merchant, token.subject_id)
    if merchant is None:
        raise HTTPException(status_code=400, detail="This link is no longer valid.")
    passwords.validate(data.newPassword, merchant.email)

    merchant.password_hash = hash_password(data.newPassword)
    merchant.password_changed_at = datetime.utcnow()
    if merchant.email_verified_at is None:
        merchant.email_verified_at = datetime.utcnow()
    session.add(merchant)
    session.commit()
    return MessageOut(message="Password updated. You can now sign in.")


@router.post("/{merchant_id}/resend-invite", response_model=MessageOut,
             dependencies=[Depends(require_admin)])
def resend_invite(merchant_id: int, session: Session = Depends(get_session)):
    """Admin: send a fresh invite link (the old one expired or never arrived)."""
    merchant = session.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="No such merchant.")
    raw = tokens.issue(session, "invite", "merchant", merchant.id)
    mailer.send_merchant_invite(merchant.email, merchant.business_name, raw)
    merchant.must_set_password = True
    session.add(merchant)
    session.commit()
    log_activity(session, "Resent merchant invite", merchant.business_name)
    return MessageOut(message=f"Invite resent to {merchant.email}.")


@router.post("/{merchant_id}/refund", response_model=BillingOut,
             dependencies=[Depends(require_admin)])
def refund_balance(merchant_id: int, data: RefundIn,
                   session: Session = Depends(get_session)):
    """Return unused prepaid balance to the merchant's card (admin action).

    Refunds against their most recent top-ups, newest first, until the amount
    is covered — so each refund reverses a real charge. The balance itself is
    reduced by the `charge.refunded` webhook, keeping Stripe the source of
    truth rather than us guessing.
    """
    merchant = session.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="No such merchant.")
    wallet = _validate_wallet(data.wallet)
    amount = round(float(data.amount), 2)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Enter an amount above £0.")

    # Refund out of the wallet being refunded, against that wallet's own
    # top-ups. Mixing them would reverse a charge for one pot while taking the
    # credit off the other.
    billing = _billing(merchant, session)
    available = next(w.balance for w in billing.wallets if w.wallet == wallet)
    if amount > available:
        raise HTTPException(
            status_code=422,
            detail=f"They only have £{available:.2f} of unused {wallet} balance.")

    topups = session.exec(
        select(MerchantTransaction)
        .where(MerchantTransaction.merchant_id == merchant_id,
               MerchantTransaction.kind == "topup",
               MerchantTransaction.wallet == wallet)
        .order_by(MerchantTransaction.id.desc())
    ).all()

    remaining = amount
    for t in topups:
        if remaining <= 0:
            break
        if not t.stripe_ref:                 # pre-Stripe demo rows can't be refunded
            continue
        take = min(remaining, t.amount)
        try:
            refund_topup(t.stripe_ref, take)
        except PaymentError as exc:
            raise HTTPException(status_code=502, detail=f"Refund failed: {exc}")
        remaining = round(remaining - take, 2)

    if remaining > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Could only refund £{amount - remaining:.2f} — the rest predates card payments.")

    log_activity(session, "Refunded merchant balance",
                 f"{merchant.business_name or merchant.email} — £{amount:.2f}")
    session.refresh(merchant)
    return _billing(merchant, session)


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
