"""Admin analytics endpoint (Phase 7).

One read-only snapshot of the whole platform for admin.html: headline numbers,
a 30-day trend, the review queue, a per-company activity table and category /
top-deal breakdowns. Everything is derived from the existing tables on read —
no new storage, no background job.

Admin-gated (reuses the campaigns X-Admin-Key).
"""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from ..cashback import admin_status, clears_at, parse_post_ts
from ..db import get_session
from ..models import (AdminAnalyticsOut, AdminCategoryStat, AdminCompanyStat,
                      AdminDealStat, AdminQueue, AdminTimePoint, Campaign,
                      CampaignSubmission, DealEvent, Mention, Merchant,
                      MerchantApplication, MerchantMessage,
                      MerchantTransaction, Receipt, User)
from .campaigns import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])

_DAYS = 30                              # trend window
_ACTIVE_DAYS = 7                        # "active" company = activity this recent
_GIVEN = ("confirmed", "paid")          # cashback the member has (or has had)
_OWED = ("pending", "verified")         # cashback still on the hook
_TOP_DEALS = 10


def _pct(part: float, whole: float) -> float:
    """part/whole as a percentage, 1dp; 0 when whole is 0."""
    return round(part / whole * 100, 1) if whole else 0.0


def _newest(*values):
    """The latest of the given datetimes, ignoring Nones."""
    real = [v for v in values if v is not None]
    return max(real) if real else None


@router.get("/analytics", response_model=AdminAnalyticsOut,
            dependencies=[Depends(require_admin)])
def analytics(session: Session = Depends(get_session)):
    now = datetime.utcnow()
    today = date.today()
    window_start = today - timedelta(days=_DAYS - 1)
    window_start_dt = datetime.combine(window_start, datetime.min.time())
    active_cutoff = now - timedelta(days=_ACTIVE_DAYS)
    month_cutoff = now - timedelta(days=_DAYS)

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
    members = session.exec(select(func.count()).select_from(User)).one()
    members_new = session.exec(
        select(func.count()).select_from(User).where(User.created_at >= month_cutoff)
    ).one()
    signups_recent = session.exec(
        select(User.created_at).where(User.created_at >= window_start_dt)
    ).all()

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
        window_start, now, active_cutoff,
    )

    return AdminAnalyticsOut(
        companiesOnboard=len(merchants),
        companiesActive30d=sum(1 for c in company_rows if c.status != "dormant"),
        companiesNew30d=sum(1 for m in merchants if m.created_at >= month_cutoff),
        companiesDormant=sum(1 for c in company_rows if c.status == "dormant"),
        applicationsTotal=len(apps),
        applicationsPending=queue.pendingApplications,
        applicationsApproved=approved,
        applicationsRejected=rejected,
        approvalRate=_pct(approved, approved + rejected),
        avgReviewHours=avg_review_hours,
        members=members,
        membersNew30d=members_new,
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
        queue=queue,
        timeseries=_timeseries(recent_events, receipts, signups_recent, window_start),
        companies=company_rows,
        categories=_categories(campaigns, receipts, status_of, views_by_deal),
        topDeals=_top_deals(campaigns, merchants, receipts, status_of,
                            views_by_deal, clicks_by_deal),
        generatedAt=now,
    )


def _timeseries(recent_events, receipts, signups, window_start) -> list:
    """Last 30 days of views/clicks/claims/signups, one point per day (zero-filled)."""
    buckets = {
        str(window_start + timedelta(days=n)):
            {"views": 0, "clicks": 0, "claims": 0, "signups": 0}
        for n in range(_DAYS)
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
                   recent_events, window_start, now, active_cutoff) -> list:
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
                  else "quiet" if last_active and last_active >= now - timedelta(days=_DAYS)
                  else "dormant")

        # Daily activity for the row's sparkline: views + clicks + claims.
        spark = {str(window_start + timedelta(days=n)): 0 for n in range(_DAYS)}
        for cid in deal_ids:
            for e in events_by_deal.get(cid, []):
                key = str(e.created_at.date())
                if key in spark:
                    spark[key] += 1
        for r in m_receipts:
            key = str(r.uploaded_at.date())
            if key in spark:
                spark[key] += 1

        rows.append(AdminCompanyStat(
            merchantId=m.id,
            name=m.business_name or m.email,
            email=m.email,
            joinedAt=m.created_at,
            logoUrl=m.logo_url,
            deals=len(deals),
            pendingSubmissions=sum(1 for s in m_subs if s.status == "pending"),
            views=views,
            clicks=clicks,
            claims=len(m_receipts),
            ctr=_pct(clicks, views),
            conversion=_pct(len(m_receipts), views),
            cashbackGiven=cashback_given,
            pendingCashback=round(
                sum(r.amount for r in m_receipts if status_of[r.id] in _OWED), 2),
            toppedUp=topped,
            balance=round(topped - cashback_given, 2),
            unreadMessages=sum(1 for msg in m_msgs
                               if msg.sender == "merchant" and not msg.read_by_admin),
            lastActiveAt=last_active,
            daysSinceActive=days_since,
            status=status,
            spark=[spark[d] for d in sorted(spark)],
        ))

    rows.sort(key=lambda c: (c.claims, c.views, c.deals), reverse=True)
    return rows


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
