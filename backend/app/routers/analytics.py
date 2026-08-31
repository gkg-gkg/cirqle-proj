"""Admin analytics endpoint (Phase 7).

One read-only snapshot of the whole platform for admin.html: headline numbers,
a 30-day trend, the review queue, a per-company activity table and category /
top-deal breakdowns. Everything is derived from the existing tables on read —
no new storage, no background job.

Admin-gated (reuses the campaigns X-Admin-Key).
"""
from collections import defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, func, select

from ..cashback import admin_status, clears_at, parse_post_ts
from ..payments import TIERS
from ..db import get_session
from ..models import (AdminAnalyticsOut, AdminCategoryStat, AdminCompanyStat,
                      AdminDealStat, AdminFraudSignal, AdminMemberStat,
                      AdminQueue, AdminTimePoint, Campaign, CampaignSubmission,
                      DealEvent, Mention, Merchant, MerchantApplication,
                      MerchantMessage, MerchantTransaction, Receipt, User)
from .campaigns import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])

_WINDOWS = (7, 30, 90)                  # selectable trend windows
_DEFAULT_DAYS = 30
_ACTIVE_DAYS = 7                        # "active" company = activity this recent
_GIVEN = ("confirmed", "paid")          # cashback the member has (or has had)
_OWED = ("pending", "verified")         # cashback still on the hook
_TOP_DEALS = 10
_TOP_MEMBERS = 25

# ── Fraud heuristics (tuned to surface a handful of rows, not a wall) ──
_REPEAT_CLAIM_MIN = 2                   # same member, same deal, this many claims
_REPEAT_CLAIM_HIGH = 4
_FRESH_ACCOUNT_MINUTES = 30             # claim filed this soon after signing up
_FRESH_ACCOUNT_HIGH_MINUTES = 5


def _pct(part: float, whole: float) -> float:
    """part/whole as a percentage, 1dp; 0 when whole is 0."""
    return round(part / whole * 100, 1) if whole else 0.0


def _newest(*values):
    """The latest of the given datetimes, ignoring Nones."""
    real = [v for v in values if v is not None]
    return max(real) if real else None


@router.get("/analytics", response_model=AdminAnalyticsOut,
            dependencies=[Depends(require_admin)])
def analytics(days: int = Query(default=_DEFAULT_DAYS,
                                description="Trend/activity window: 7, 30 or 90 days."),
              session: Session = Depends(get_session)):
    window_days = days if days in _WINDOWS else _DEFAULT_DAYS
    now = datetime.utcnow()
    today = date.today()
    window_start = today - timedelta(days=window_days - 1)
    window_start_dt = datetime.combine(window_start, datetime.min.time())
    active_cutoff = now - timedelta(days=_ACTIVE_DAYS)
    window_cutoff = now - timedelta(days=window_days)

    merchants = session.exec(select(Merchant)).all()
    campaigns = session.exec(select(Campaign)).all()
    apps = session.exec(select(MerchantApplication)).all()
    receipts = session.exec(select(Receipt)).all()
    subs = session.exec(select(CampaignSubmission)).all()
    messages = session.exec(select(MerchantMessage)).all()
    topups = session.exec(select(MerchantTransaction)).all()

    # ── Deal events ──
    # The event log is the biggest table, so it is aggregated in the database
    # rather than pulled into memory. Only the trend window is fetched row-wise.
    views_by_deal, clicks_by_deal = {}, {}
    for cid, kind, n in session.exec(
        select(DealEvent.campaign_id, DealEvent.kind, func.count())
        .group_by(DealEvent.campaign_id, DealEvent.kind)
    ).all():
        if kind == "view":
            views_by_deal[cid] = n
        elif kind == "click":
            clicks_by_deal[cid] = n
    last_event_by_deal = dict(session.exec(
        select(DealEvent.campaign_id, func.max(DealEvent.created_at))
        .group_by(DealEvent.campaign_id)
    ).all())
    recent_events = session.exec(
        select(DealEvent).where(DealEvent.created_at >= window_start_dt)
    ).all()

    # ── Claim statuses ──
    # A claim's real status depends on the tagged post's date (see cashback.py),
    # so resolve each one once here and reuse it everywhere below.
    post_ids = {r.post_id for r in receipts if r.post_id}
    post_ts = {}
    if post_ids:
        post_ts = {m.id: parse_post_ts(m.timestamp) for m in session.exec(
            select(Mention).where(Mention.id.in_(post_ids))
        ).all()}
    status_of = {r.id: admin_status(r, post_ts.get(r.post_id)) for r in receipts}

    # ── Members ──
    users = session.exec(select(User)).all()
    members_new = sum(1 for u in users if u.created_at >= window_cutoff)
    signups_recent = [u.created_at for u in users if u.created_at >= window_start_dt]
    posts_by_user = dict(session.exec(
        select(Mention.user_id, func.count()).group_by(Mention.user_id)
    ).all())

    # ── Headline engagement + money ──
    views = sum(views_by_deal.values())
    clicks = sum(clicks_by_deal.values())
    claims = len(receipts)
    given = [r for r in receipts if status_of[r.id] in _GIVEN]
    cashback_given = round(sum(r.amount for r in given), 2)
    pending_cashback = round(
        sum(r.amount for r in receipts if status_of[r.id] in _OWED), 2)
    expired_cashback = round(
        sum(r.amount for r in receipts if status_of[r.id] == "expired"), 2)
    topped_up = round(sum(t.amount for t in topups), 2)

    # ── Review queue ──
    expiring_soon = sum(
        1 for r in receipts
        if status_of[r.id] == "pending"
        and now < clears_at(r, post_ts.get(r.post_id)) <= now + timedelta(days=1)
    )
    queue = AdminQueue(
        pendingReceipts=sum(1 for r in receipts if status_of[r.id] == "pending"),
        pendingMembers=sum(1 for u in users if u.status == "pending"),
        pendingApplications=sum(1 for a in apps if a.status == "pending"),
        pendingSubmissions=sum(1 for s in subs if s.status == "pending"),
        unreadMessages=sum(1 for m in messages
                           if m.sender == "merchant" and not m.read_by_admin),
        expiringSoon=expiring_soon,
    )

    # ── Application pipeline ──
    approved = sum(1 for a in apps if a.status == "approved")
    rejected = sum(1 for a in apps if a.status == "rejected")
    reviewed = [a for a in apps if a.reviewed_at]
    avg_review_hours = round(
        sum((a.reviewed_at - a.created_at).total_seconds() for a in reviewed)
        / len(reviewed) / 3600, 1) if reviewed else None

    company_rows = _company_stats(
        merchants, campaigns, receipts, status_of, subs, messages, topups,
        views_by_deal, clicks_by_deal, last_event_by_deal, recent_events,
        window_start, window_days, now, active_cutoff, window_cutoff,
    )
    member_rows = _member_stats(users, receipts, status_of, posts_by_user)

    return AdminAnalyticsOut(
        companiesOnboard=len(merchants),
        companiesActiveInWindow=sum(1 for c in company_rows if c.status != "dormant"),
        companiesNewInWindow=sum(1 for m in merchants if m.created_at >= window_cutoff),
        companiesDormant=sum(1 for c in company_rows if c.status == "dormant"),
        applicationsTotal=len(apps),
        applicationsPending=queue.pendingApplications,
        applicationsApproved=approved,
        applicationsRejected=rejected,
        approvalRate=_pct(approved, approved + rejected),
        avgReviewHours=avg_review_hours,
        members=len(users),
        membersNewInWindow=members_new,
        deals=len(campaigns),
        dealsFromCompanies=sum(1 for c in campaigns if c.merchant_id),
        views=views,
        clicks=clicks,
        claims=claims,
        ctr=_pct(clicks, views),
        conversion=_pct(claims, views),
        cashbackGiven=cashback_given,
        pendingCashback=pending_cashback,
        expiredCashback=expired_cashback,
        rejectedClaims=sum(1 for r in receipts if status_of[r.id] == "rejected"),
        toppedUp=topped_up,
        outstandingBalance=round(topped_up - cashback_given, 2),
        avgClaimValue=round(cashback_given / len(given), 2) if given else 0.0,
        companiesAtRisk=sum(1 for c in company_rows if c.atRisk),
        totalShortfall=round(sum(c.shortfall for c in company_rows), 2),
        walletOwed=round(sum(r.amount for r in receipts
                             if status_of[r.id] == "confirmed"), 2),
        paidOut=round(sum(r.amount for r in receipts
                          if status_of[r.id] == "paid"), 2),
        windowDays=window_days,
        queue=queue,
        timeseries=_timeseries(recent_events, receipts, signups_recent,
                               window_start, window_days),
        companies=company_rows,
        topMembers=member_rows[:_TOP_MEMBERS],
        fraud=_fraud_signals(users, receipts, status_of, campaigns, now),
        categories=_categories(campaigns, receipts, status_of, views_by_deal),
        topDeals=_top_deals(campaigns, merchants, receipts, status_of,
                            views_by_deal, clicks_by_deal),
        generatedAt=now,
    )


def _timeseries(recent_events, receipts, signups, window_start, window_days) -> list:
    """The window's views/clicks/claims/signups, one point per day (zero-filled)."""
    buckets = {
        str(window_start + timedelta(days=n)):
            {"views": 0, "clicks": 0, "claims": 0, "signups": 0}
        for n in range(window_days)
    }

    def bump(when, key):
        bucket = buckets.get(str(when.date()))
        if bucket is not None:
            bucket[key] += 1

    for e in recent_events:
        if e.kind in ("view", "click"):
            bump(e.created_at, e.kind + "s")
    for r in receipts:
        bump(r.uploaded_at, "claims")
    for created_at in signups:
        bump(created_at, "signups")

    return [AdminTimePoint(date=d, **buckets[d]) for d in sorted(buckets)]


def _company_stats(merchants, campaigns, receipts, status_of, subs, messages,
                   topups, views_by_deal, clicks_by_deal, last_event_by_deal,
                   recent_events, window_start, window_days, now, active_cutoff,
                   window_cutoff) -> list:
    """One activity row per onboarded company, busiest first."""
    deals_by_merchant = {}
    for c in campaigns:
        if c.merchant_id:
            deals_by_merchant.setdefault(c.merchant_id, []).append(c)
    receipts_by_deal = {}
    for r in receipts:
        if r.campaign_id:
            receipts_by_deal.setdefault(r.campaign_id, []).append(r)
    events_by_deal = {}
    for e in recent_events:
        events_by_deal.setdefault(e.campaign_id, []).append(e)

    rows = []
    for m in merchants:
        deals = deals_by_merchant.get(m.id, [])
        deal_ids = [c.id for c in deals]
        m_receipts = [r for cid in deal_ids for r in receipts_by_deal.get(cid, [])]
        m_subs = [s for s in subs if s.merchant_id == m.id]
        m_msgs = [msg for msg in messages if msg.merchant_id == m.id]
        m_topups = [t for t in topups if t.merchant_id == m.id]

        views = sum(views_by_deal.get(cid, 0) for cid in deal_ids)
        clicks = sum(clicks_by_deal.get(cid, 0) for cid in deal_ids)
        cashback_given = round(
            sum(r.amount for r in m_receipts if status_of[r.id] in _GIVEN), 2)
        topped = round(sum(t.amount for t in m_topups), 2)

        last_active = _newest(
            m.created_at,
            *[last_event_by_deal.get(cid) for cid in deal_ids],
            *[r.uploaded_at for r in m_receipts],
            *[s.created_at for s in m_subs],
            *[msg.created_at for msg in m_msgs if msg.sender == "merchant"],
        )
        days_since = (now - last_active).days if last_active else None
        status = ("active" if last_active and last_active >= active_cutoff
                  else "quiet" if last_active and last_active >= window_cutoff
                  else "dormant")

        # Daily activity for the row's sparkline: views + clicks + claims.
        spark = {str(window_start + timedelta(days=n)): 0 for n in range(window_days)}
        for cid in deal_ids:
            for e in events_by_deal.get(cid, []):
                key = str(e.created_at.date())
                if key in spark:
                    spark[key] += 1
        for r in m_receipts:
            key = str(r.uploaded_at.date())
            if key in spark:
                spark[key] += 1

        pending_cashback = round(
            sum(r.amount for r in m_receipts if status_of[r.id] in _OWED), 2)
        balance = round(topped - cashback_given, 2)
        shortfall = round(max(0.0, pending_cashback - balance), 2)

        rows.append(AdminCompanyStat(
            merchantId=m.id,
            name=m.business_name or m.email,
            email=m.email,
            joinedAt=m.created_at,
            tier=m.tier or "",
            planName=(TIERS.get(m.tier or "") or {}).get("name", ""),
            subscriptionStatus=m.subscription_status or "none",
            logoUrl=m.logo_url,
            deals=len(deals),
            pendingSubmissions=sum(1 for s in m_subs if s.status == "pending"),
            views=views,
            clicks=clicks,
            claims=len(m_receipts),
            ctr=_pct(clicks, views),
            conversion=_pct(len(m_receipts), views),
            cashbackGiven=cashback_given,
            pendingCashback=pending_cashback,
            toppedUp=topped,
            balance=balance,
            unreadMessages=sum(1 for msg in m_msgs
                               if msg.sender == "merchant" and not msg.read_by_admin),
            shortfall=shortfall,
            atRisk=shortfall > 0,
            lastActiveAt=last_active,
            daysSinceActive=days_since,
            status=status,
            spark=[spark[d] for d in sorted(spark)],
        ))

    rows.sort(key=lambda c: (c.claims, c.views, c.deals), reverse=True)
    return rows


def _member_stats(users, receipts, status_of, posts_by_user) -> list:
    """One ledger row per member, biggest earners first."""
    by_user = defaultdict(list)
    for r in receipts:
        by_user[r.user_id].append(r)

    rows = []
    for u in users:
        mine = by_user.get(u.id, [])
        st = [status_of[r.id] for r in mine]
        total = lambda kinds: round(  # noqa: E731 — local shorthand, used 4 times below
            sum(r.amount for r, s in zip(mine, st) if s in kinds), 2)
        wallet = total(("confirmed",))
        paid = total(("paid",))
        rows.append(AdminMemberStat(
            userId=u.id,
            name=f"{u.first_name} {u.last_name}".strip() or u.email,
            email=u.email,
            instagramHandle=u.instagram_handle or "",
            status=u.status,
            joinedAt=u.created_at,
            posts=posts_by_user.get(u.id, 0),
            claims=len(mine),
            earned=round(wallet + paid, 2),
            wallet=wallet,
            paidOut=paid,
            pending=total(_OWED),
            expired=total(("expired",)),
            rejected=sum(1 for x in st if x == "rejected"),
            brandsUsed=len({r.brand for r in mine if r.brand}),
            lastClaimAt=max((r.uploaded_at for r in mine), default=None),
        ))

    rows.sort(key=lambda m: (m.earned, m.claims), reverse=True)
    return rows


def _fraud_signals(users, receipts, status_of, campaigns, now) -> list:
    """Claims worth a second look before cashback goes out.

    Every signal here is a heuristic with an innocent explanation — a member
    genuinely can post twice about one brand — so these are surfaced for review,
    never auto-rejected. Only claims that can still cost money are considered
    (rejected and expired ones are already dead).
    """
    live = [r for r in receipts if status_of[r.id] not in ("rejected", "expired")]
    user_by_id = {u.id: u for u in users}
    title_by_campaign = {c.id: (c.card_title or c.title or c.brand) for c in campaigns}

    def who(user_id):
        u = user_by_id.get(user_id)
        return f"{u.first_name} {u.last_name} — {u.email}".strip(" —") if u else f"user #{user_id}"

    signals = []

    # 1. The same member claiming the same deal over and over.
    by_user_deal = defaultdict(list)
    for r in live:
        if r.campaign_id:
            by_user_deal[(r.user_id, r.campaign_id)].append(r)
    for (user_id, campaign_id), rs in by_user_deal.items():
        if len(rs) < _REPEAT_CLAIM_MIN:
            continue
        deal = title_by_campaign.get(campaign_id, f"deal #{campaign_id}")
        signals.append(AdminFraudSignal(
            kind="repeat_claims",
            severity="high" if len(rs) >= _REPEAT_CLAIM_HIGH else "watch",
            title=f"{len(rs)} claims on one deal by one member",
            detail=f"{deal} — {len(rs)} separate claims from the same account.",
            member=who(user_id),
            amount=round(sum(r.amount for r in rs), 2),
            count=len(rs),
            receiptIds=[r.id for r in rs],
        ))

    # 2. Accounts that claimed almost immediately after signing up.
    for r in live:
        u = user_by_id.get(r.user_id)
        if not u:
            continue
        gap = (r.uploaded_at - u.created_at).total_seconds() / 60
        if 0 <= gap <= _FRESH_ACCOUNT_MINUTES:
            mins = int(gap)
            signals.append(AdminFraudSignal(
                kind="fresh_account",
                severity="high" if gap <= _FRESH_ACCOUNT_HIGH_MINUTES else "watch",
                title=f"Claim filed {mins} min after signup",
                detail=f"{r.brand or 'Cashback'} £{r.amount:.2f} — account created "
                       f"{u.created_at:%d %b %Y %H:%M}, claim uploaded {mins} minute(s) later.",
                member=who(r.user_id),
                amount=round(r.amount, 2),
                count=1,
                receiptIds=[r.id],
            ))

    # 3. The same image file used on more than one claim. Byte-identical only —
    # a re-saved or cropped copy hashes differently and won't show up here.
    by_hash = defaultdict(list)
    for r in live:
        if r.image_sha256:
            by_hash[r.image_sha256].append(r)
    for digest, rs in by_hash.items():
        if len(rs) < 2:
            continue
        members = {r.user_id for r in rs}
        signals.append(AdminFraudSignal(
            kind="duplicate_image",
            severity="high" if len(members) > 1 else "watch",
            title=f"Same receipt image on {len(rs)} claims",
            detail=(f"Identical file across {len(rs)} claims from {len(members)} "
                    f"account(s) — hash {digest[:12]}…"),
            member=" · ".join(who(uid) for uid in sorted(members)),
            amount=round(sum(r.amount for r in rs), 2),
            count=len(rs),
            receiptIds=[r.id for r in rs],
        ))

    # 4. One Instagram post claimed by several different accounts.
    by_post = defaultdict(list)
    for r in live:
        if r.post_id:
            by_post[r.post_id].append(r)
    for post_id, rs in by_post.items():
        members = {r.user_id for r in rs}
        if len(members) < 2:
            continue
        signals.append(AdminFraudSignal(
            kind="shared_post",
            severity="high",
            title=f"One post claimed by {len(members)} accounts",
            detail=f"Instagram post {post_id} backs claims from {len(members)} different accounts.",
            member=" · ".join(who(uid) for uid in sorted(members)),
            amount=round(sum(r.amount for r in rs), 2),
            count=len(rs),
            receiptIds=[r.id for r in rs],
        ))

    signals.sort(key=lambda f: (f.severity != "high", -f.amount, -f.count))
    return signals


def _categories(campaigns, receipts, status_of, views_by_deal) -> list:
    """Deals / views / claims / cashback grouped by deal category."""
    by_deal_category = {c.id: (c.category or "Uncategorised") for c in campaigns}
    stats = {}
    for c in campaigns:
        s = stats.setdefault(by_deal_category[c.id],
                             {"deals": 0, "views": 0, "claims": 0, "cashback": 0.0})
        s["deals"] += 1
        s["views"] += views_by_deal.get(c.id, 0)
    for r in receipts:
        cat = by_deal_category.get(r.campaign_id)
        if cat is None:
            continue
        stats[cat]["claims"] += 1
        if status_of[r.id] in _GIVEN:
            stats[cat]["cashback"] += r.amount

    rows = [AdminCategoryStat(category=cat, deals=s["deals"], views=s["views"],
                              claims=s["claims"], cashback=round(s["cashback"], 2))
            for cat, s in stats.items()]
    rows.sort(key=lambda c: (c.claims, c.views, c.deals), reverse=True)
    return rows


def _top_deals(campaigns, merchants, receipts, status_of,
               views_by_deal, clicks_by_deal) -> list:
    """The busiest deals across the whole catalog."""
    company_of = {m.id: (m.business_name or m.email) for m in merchants}
    receipts_by_deal = {}
    for r in receipts:
        if r.campaign_id:
            receipts_by_deal.setdefault(r.campaign_id, []).append(r)

    rows = []
    for c in campaigns:
        c_receipts = receipts_by_deal.get(c.id, [])
        rows.append(AdminDealStat(
            campaignId=c.id,
            brand=c.brand,
            title=c.card_title or c.title or c.brand,
            company=company_of.get(c.merchant_id, ""),
            views=views_by_deal.get(c.id, 0),
            clicks=clicks_by_deal.get(c.id, 0),
            claims=len(c_receipts),
            cashback=round(sum(r.amount for r in c_receipts
                               if status_of[r.id] in _GIVEN), 2),
        ))
    rows.sort(key=lambda d: (d.claims, d.views, d.clicks), reverse=True)
    return rows[:_TOP_DEALS]
