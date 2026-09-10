"""The referral competition — seasons, scores, streaks, and standings.

One number decides everything here: how many people a member has referred whose
purchase actually went through. Nothing about money enters the score, and a
sign-up on its own is worth zero — a referral only counts once the person they
referred has bought something and that claim has cleared. That is what keeps a
cash prize for referring people on the right side of the line between a
competition and a scheme that pays for recruitment.

    score  = settled referrals in the season
    streak = consecutive Mon-Sun weeks with at least one referral clearing

WHEN A REFERRAL HAPPENS
-----------------------
Not when its `ReferralReward` row was written. Referrals settle lazily, the
first time anybody loads a page that needs them (see referrals.settle_for_user),
so `created_at` records when a dashboard was opened — which for a weekly streak
would invent gaps and bunch three weeks of referrals into whichever afternoon
someone happened to log in.

The honest moment is when the referred claim's cashback cleared:
CONFIRM_DAYS after the post date, computed by cashback.clears_at(). It is fixed
by the post, identical on every read, and it is the same moment the member was
already told their cashback would land.

WHY WEEKS GET THREE DAYS OF GRACE
---------------------------------
Because clearing lags the purchase by CONFIRM_DAYS, a referral earned on Sunday
does not clear until Wednesday. Ending a week the moment it ends would break
streaks that the member had in fact kept. So a week is only allowed to BREAK a
streak once it has settled — GRACE_DAYS after it ends. Until then an empty week
is skipped over: it cannot add to the streak yet, but it cannot end it either.

NOTHING HERE RUNS ON A TIMER
----------------------------
There is no scheduler in this codebase. A running season is recomputed on every
read. A finished one is frozen — computed once by whichever request arrives
first after the freeze date, then stored and never recomputed — because a prize
hangs off the final table and a rank must not move after someone has been told
they won.
"""
from datetime import date, datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from .cashback import clears_at, parse_post_ts
from .models import Mention, Receipt, ReferralReward, SeasonStanding, User

# A season is a calendar quarter. Referrals are rare enough per member that a
# monthly table would be mostly zeroes and ties.
SEASON_MONTHS = 3

# How long after a season ends before its table is final: CONFIRM_DAYS for the
# last purchases to clear, plus room for the admin review queue to drain.
FREEZE_DAYS = 8

# How long after a week ends before an empty one is allowed to break a streak.
GRACE_DAYS = 3

# What the competition pays, and the floor for being paid at all. Ranks 1-10
# with no referrals would hand out money for doing nothing, so a member has to
# have referred at least one person to place.
PRIZE_PLACES = 10
PRIZE_AMOUNT = 10.0
MIN_SCORE_TO_PLACE = 1


# ── Seasons ──────────────────────────────────────────────────────────────────

def season_of(when: date) -> str:
    """The season label a date falls in, e.g. 2026-Q3."""
    return f"{when.year}-Q{(when.month - 1) // SEASON_MONTHS + 1}"


def season_bounds(season: str) -> tuple[date, date]:
    """First and last day of a season, inclusive."""
    year, quarter = season.split("-Q")
    year, quarter = int(year), int(quarter)
    first_month = (quarter - 1) * SEASON_MONTHS + 1
    start = date(year, first_month, 1)
    end_month = first_month + SEASON_MONTHS
    end = (date(year + 1, 1, 1) if end_month > 12
           else date(year, end_month, 1)) - timedelta(days=1)
    return start, end


def freezes_on(season: str) -> date:
    """The day a finished season's table becomes final."""
    return season_bounds(season)[1] + timedelta(days=FREEZE_DAYS)


def is_final(season: str, today: date) -> bool:
    """Has this season closed AND had time for its last claims to clear?"""
    return today >= freezes_on(season)


def current_season(today: Optional[date] = None) -> str:
    return season_of(today or datetime.utcnow().date())


# ── When each referral cleared ───────────────────────────────────────────────

def _settled_rewards(session: Session,
                     user_id: Optional[int] = None) -> list[ReferralReward]:
    """Every referral bonus paid to a REFERRER that hasn't been cancelled.

    The referee's own 50p is deliberately excluded: being referred is not an
    achievement, and counting it would let two people farm each other.

    `user_id` narrows it to one member — the dashboard needs one person's
    figures and has no reason to read the whole competition to get them.
    """
    query = select(ReferralReward).where(
        ReferralReward.kind == "referrer",
        ReferralReward.status != "cancelled",
    )
    if user_id is not None:
        query = query.where(ReferralReward.user_id == user_id)
    return list(session.exec(query).all())


def _cleared_at(rewards: list[ReferralReward],
                session: Session) -> dict[int, datetime]:
    """reward id -> the moment the referral behind it cleared.

    Batched rather than per-reward: this runs for every member on the
    leaderboard at once, and a lookup each would be three queries per row.
    """
    receipt_ids = {r.receipt_id for r in rewards}
    if not receipt_ids:
        return {}
    receipts = {
        r.id: r for r in session.exec(
            select(Receipt).where(Receipt.id.in_(receipt_ids))).all()
    }
    # Visit claims carry post_id "" and have no Mention; they clear from their
    # upload date instead, which clears_at() already handles.
    post_ids = {r.post_id for r in receipts.values() if r.post_id}
    mentions = {
        m.id: m for m in session.exec(
            select(Mention).where(Mention.id.in_(post_ids))).all()
    } if post_ids else {}

    out: dict[int, datetime] = {}
    for reward in rewards:
        receipt = receipts.get(reward.receipt_id)
        if receipt is None:              # claim deleted out from under us
            continue
        mention = mentions.get(receipt.post_id)
        out[reward.id] = clears_at(
            receipt, parse_post_ts(mention.timestamp) if mention else None)
    return out


def clearing_times(session: Session,
                   user_id: Optional[int] = None) -> dict[int, list[datetime]]:
    """user id -> the moments their referrals cleared, oldest first."""
    rewards = _settled_rewards(session, user_id)
    moments = _cleared_at(rewards, session)
    out: dict[int, list[datetime]] = {}
    for reward in rewards:
        moment = moments.get(reward.id)
        if moment is not None:
            out.setdefault(reward.user_id, []).append(moment)
    for times in out.values():
        times.sort()
    return out


# ── Streaks ──────────────────────────────────────────────────────────────────

def week_start(when: date) -> date:
    """The Monday of the week this date falls in."""
    return when - timedelta(days=when.weekday())


def _week_settled(monday: date, today: date) -> bool:
    """Has this week ended long enough ago to be trusted as empty?"""
    return today >= monday + timedelta(days=7 + GRACE_DAYS)


def streak_weeks(times: list[datetime], today: date) -> int:
    """Consecutive weeks, ending at this one, with a referral in each.

    Walks back from the current week. An empty week that has settled ends the
    walk; an empty week still inside its grace period is stepped over, because
    a referral earned in it may not have cleared yet.
    """
    if not times:
        return 0
    active = {week_start(t.date()) for t in times}
    floor = min(active)

    streak = 0
    monday = week_start(today)
    while monday >= floor:
        if monday in active:
            streak += 1
        elif _week_settled(monday, today):
            break
        monday -= timedelta(days=7)
    return streak


# ── Scores and standings ─────────────────────────────────────────────────────

def _in_season(times: list[datetime], season: str) -> list[datetime]:
    start, end = season_bounds(season)
    return [t for t in times if start <= t.date() <= end]


def score_for(times: list[datetime], season: str) -> int:
    """How many referrals this member landed in the season."""
    return len(_in_season(times, season))


def display_name(user: User) -> str:
    """What to call a member in public. Their own choice first, then the handle
    they already show in the feed, then a first name."""
    if user.display_name:
        return user.display_name
    if user.instagram_handle:
        return f"@{user.instagram_handle}"
    return user.first_name or "Member"


def _rank_rows(session: Session, season: str) -> list[dict]:
    """The season's table, best first, for members who have opted in.

    Ties are broken by who got there FIRST — the earliest clearing time of the
    referral that took them to their final count. Splitting a £10 prize between
    two people is worse than an arbitrary-looking but consistent rule, and this
    one is at least a rule the member can understand.
    """
    times = clearing_times(session)
    if not times:
        return []
    users = {
        u.id: u for u in session.exec(
            select(User).where(User.id.in_(set(times)))).all()
    }

    rows = []
    for user_id, moments in times.items():
        user = users.get(user_id)
        if user is None or not user.leaderboard_opt_in:
            continue
        in_season = _in_season(moments, season)
        if not in_season:
            continue
        rows.append({
            "userId": user_id,
            "name": display_name(user),
            "score": len(in_season),
            "reached": max(in_season),      # when they hit that count
        })

    rows.sort(key=lambda r: (-r["score"], r["reached"]))
    for place, row in enumerate(rows, start=1):
        row["rank"] = place
    return rows


def wins_prize(rank: int, score: int) -> bool:
    return rank <= PRIZE_PLACES and score >= MIN_SCORE_TO_PLACE


# ── Freezing ─────────────────────────────────────────────────────────────────

def _frozen_rows(session: Session, season: str) -> list[dict]:
    """A finished season's stored table, or [] if it was never written."""
    stored = session.exec(
        select(SeasonStanding)
        .where(SeasonStanding.season == season)
        .order_by(SeasonStanding.rank)
    ).all()
    if not stored:
        return []
    users = {
        u.id: u for u in session.exec(
            select(User).where(User.id.in_({s.user_id for s in stored}))).all()
    }
    return [
        {
            "userId": s.user_id,
            # Re-read the name so a later rename shows through; the rank and
            # score are what had to be frozen, not what someone calls themself.
            "name": display_name(users[s.user_id]) if s.user_id in users
                    else "Former member",
            "score": s.score,
            "rank": s.rank,
        }
        for s in stored
    ]


def freeze(session: Session, season: str) -> list[dict]:
    """Write a finished season's table once, then leave it alone forever."""
    rows = _rank_rows(session, season)
    now = datetime.utcnow()
    for row in rows:
        session.add(SeasonStanding(
            season=season, user_id=row["userId"],
            rank=row["rank"], score=row["score"], frozen_at=now,
        ))
    session.commit()
    return rows


def standings(session: Session, season: str,
              today: Optional[date] = None) -> tuple[list[dict], bool]:
    """The season's table and whether it is final.

    A running season is computed fresh. A finished one is served from
    SeasonStanding, written by whichever request got here first after the
    freeze date.
    """
    today = today or datetime.utcnow().date()
    if not is_final(season, today):
        return _rank_rows(session, season), False

    stored = _frozen_rows(session, season)
    if stored:
        return stored, True
    return freeze(session, season), True
