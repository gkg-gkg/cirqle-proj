"""A merchant's customers, and whether they come back (Phase B).

Everything the merchant dashboard knows about people rather than claims is
worked out here: who bought, how often, how much, and — the question the whole
platform turns on — whether the ones who arrived through a referral stay longer
than the ones who found the deal themselves.

Two things shape the maths.

A claim needs an Instagram post (see routers/receipts.py: post_id is required
and a claim is unique on (user_id, post_id)), so what is measured below is
repeat POSTING, not repeat visiting. Someone who comes back four times without
posting counts once. The figures are therefore a floor, not a census, and the
portal says so rather than quietly implying otherwise.

Nothing here runs on a timer, matching how cashback and referral bonuses already
work in this codebase — it is all computed when the dashboard is read. At
current volume that is the right trade: no scheduler exists to hang a nightly
rollup off, and inventing one to serve a page nobody has loaded yet would be
speculative. `build_customers` is the seam where a stored rollup slots in later
without any of the callers changing.
"""
from datetime import date, datetime, timedelta
from statistics import median
from typing import Optional

from sqlmodel import Session, select

from .models import Campaign, Receipt, ReferralReward, User

# Claims that actually earned the member their cashback. A rejected or still
# pending claim is not a customer the merchant got anything from.
CREDITED = ("confirmed", "paid")

# Receipt currencies that count toward spend — see routers/merchant.py, same
# reasoning: a blank is a UK receipt that printed no code.
SPEND_CURRENCIES = ("", "GBP")

# A second claim inside this window is the same trip claimed twice, or one
# weekend, not a customer choosing to come back. Without the floor the repeat
# rate flatters itself into uselessness.
STICKY_MIN_DAYS = 7

# Cohort cells and split-by-path figures below this many customers are withheld.
# A merchant with three claims reading "33% repeat rate" is reading noise.
MIN_COHORT = 5

# How far out the cohort grid reaches.
COHORT_MONTHS = 5

# "Still going" — a claim this long after their first.
LONG_RUN_DAYS = 90

# A customer is lapsed once they have been quiet for this multiple of their own
# usual gap between claims.
LAPSE_MULTIPLE = 2


def happened_on(receipt: Receipt) -> date:
    """The day the purchase happened.

    The receipt's own printed date when it was legible, because that is when the
    customer actually walked in. Otherwise the upload date, which is the best
    remaining evidence — a member can upload several receipts in one sitting, so
    it is a worse signal, but dropping the claim entirely would lose more than
    it protects.
    """
    return receipt.purchase_date or receipt.uploaded_at.date()


def spend_of(receipt: Receipt) -> float:
    """£ this claim contributes to a merchant's revenue. 0 when unreadable."""
    if receipt.basket_total is None:
        return 0.0
    if receipt.basket_currency not in SPEND_CURRENCIES:
        return 0.0
    return receipt.basket_total


class CustomerRecord:
    """One person's whole history with one merchant, assembled from their claims.

    A plain object rather than a table: it is derived data, and keeping it
    derived means it can never drift out of step with the receipts it came from.
    """

    def __init__(self, user: User):
        self.user_id = user.id
        self.handle = user.instagram_handle or ""
        self.days: list[date] = []          # one per credited claim, sorted
        self.spend = 0.0
        self.cashback = 0.0
        self.referred_by_id: Optional[int] = None
        self.referred_by_handle = ""
        self.referred_count = 0             # people THEY brought to this merchant

    # ── Shape of their history ──
    @property
    def claims(self) -> int:
        return len(self.days)

    @property
    def first(self) -> date:
        return self.days[0]

    @property
    def last(self) -> date:
        return self.days[-1]

    @property
    def acquisition(self) -> str:
        """How they arrived: through someone else's post, or on their own."""
        return "referred" if self.referred_by_id else "direct"

    @property
    def sticky(self) -> bool:
        """Came back — a second claim at least STICKY_MIN_DAYS after the first."""
        return (self.claims >= 2
                and (self.days[1] - self.days[0]).days >= STICKY_MIN_DAYS)

    @property
    def days_to_second(self) -> Optional[int]:
        return (self.days[1] - self.days[0]).days if self.claims >= 2 else None

    @property
    def gaps(self) -> list[int]:
        """Days between each of their consecutive claims."""
        return [(b - a).days for a, b in zip(self.days, self.days[1:])]

    def returned_within(self, days: int) -> bool:
        """Did they claim again inside `days` of their first?"""
        return any(0 < (d - self.first).days <= days for d in self.days[1:])

    def still_going_after(self, days: int) -> bool:
        """Did they claim at all this long after their first?"""
        return any((d - self.first).days >= days for d in self.days[1:])

    def matured(self, days: int, today: date) -> bool:
        """Has enough time passed to judge them over this window?

        Asking whether a customer who first bought yesterday has returned within
        90 days is a question with no answer yet, and counting them as a "no"
        is what makes a healthy dashboard look like it is collapsing.
        """
        return (today - self.first).days >= days


def build_customers(merchant_id: int, session: Session) -> list[CustomerRecord]:
    """Every person who has earned cashback on this merchant's deals.

    One pass over the merchant's credited claims. Sorted by spend, so the
    customers list arrives most-valuable-first without the caller sorting again.
    """
    campaign_ids = [
        c.id for c in session.exec(
            select(Campaign).where(Campaign.merchant_id == merchant_id)).all()
    ]
    if not campaign_ids:
        return []

    receipts = session.exec(
        select(Receipt).where(
            Receipt.campaign_id.in_(campaign_ids),
            Receipt.status.in_(CREDITED),
        )
    ).all()
    if not receipts:
        return []

    users = {
        u.id: u for u in session.exec(
            select(User).where(User.id.in_({r.user_id for r in receipts}))).all()
    }

    records: dict[int, CustomerRecord] = {}
    for r in receipts:
        user = users.get(r.user_id)
        if user is None:                     # account deleted out from under us
            continue
        rec = records.get(r.user_id)
        if rec is None:
            rec = records[r.user_id] = CustomerRecord(user)
        rec.days.append(happened_on(r))
        rec.spend += spend_of(r)
        rec.cashback += r.amount
        # Who brought them. Their first named referrer wins: someone can be
        # referred once, and a later claim naming a different member does not
        # re-acquire a customer the merchant already had.
        if r.referred_by_user_id and rec.referred_by_id is None:
            rec.referred_by_id = r.referred_by_user_id
            rec.referred_by_handle = r.referred_by_handle

    for rec in records.values():
        rec.days.sort()
        rec.spend = round(rec.spend, 2)
        rec.cashback = round(rec.cashback, 2)

    # How many people each of them brought in, counted only among customers of
    # THIS merchant — a merchant never learns what a member does elsewhere.
    for rec in records.values():
        if rec.referred_by_id in records:
            records[rec.referred_by_id].referred_count += 1

    return sorted(records.values(), key=lambda c: c.spend, reverse=True)


# ── The headline retention figures ───────────────────────────────────────────

def _pct(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def repeat_rate(records: list[CustomerRecord], today: date) -> float:
    """Share of customers who came back, judged only on matured ones.

    A customer who first bought last week has not failed to return; they have
    not had the chance. Counting them would drag the rate down every time the
    merchant acquires someone new — the exact opposite of the truth.
    """
    judged = [c for c in records if c.matured(STICKY_MIN_DAYS, today)]
    return _pct(sum(1 for c in judged if c.sticky), len(judged))


def return_rate(records: list[CustomerRecord], days: int, today: date) -> float:
    """Share returning inside `days` of their first claim, matured cohorts only."""
    judged = [c for c in records if c.matured(days, today)]
    return _pct(sum(1 for c in judged if c.returned_within(days)), len(judged))


def third_claim_rate(records: list[CustomerRecord]) -> float:
    """Of those who came back once, how many came back again.

    Visit three is where the industry consistently sees the relationship lock
    in, which makes this the most useful single number on the page after the
    repeat rate itself.
    """
    repeaters = [c for c in records if c.claims >= 2]
    return _pct(sum(1 for c in repeaters if c.claims >= 3), len(repeaters))


def median_days_to_second(records: list[CustomerRecord]) -> Optional[int]:
    """Typical wait before a customer's second claim. None if nobody has one.

    Worth watching closely: it moves months before the repeat rate does, because
    it responds to the customers arriving now rather than to the whole history.
    """
    waits = [c.days_to_second for c in records if c.days_to_second is not None]
    return int(median(waits)) if waits else None


def lapsed(records: list[CustomerRecord], today: date) -> list[CustomerRecord]:
    """Customers who have gone quiet for far longer than they usually do.

    Judged against each person's own rhythm where they have one, because a
    weekly regular who vanishes for a month is a problem and a twice-a-year
    customer doing the same is not. Customers with a single claim are judged
    against the merchant's typical gap instead, since they have no rhythm yet.
    """
    all_gaps = [g for c in records for g in c.gaps]
    typical = median(all_gaps) if all_gaps else 0
    out = []
    for c in records:
        own = median(c.gaps) if c.gaps else typical
        if own <= 0:
            continue                          # no rhythm anywhere yet — say nothing
        if (today - c.last).days > own * LAPSE_MULTIPLE:
            out.append(c)
    return out


def split_by_path(records: list[CustomerRecord], today: date) -> dict:
    """Retention for referred customers vs those who found the deal themselves.

    This is the comparison no cashback platform or loyalty CDP can make — they
    know who came back, but not which acquisition path produced them, because
    they do not hold the referral graph. Suppressed below MIN_COHORT so a
    merchant never reads a two-person sample as a finding.
    """
    out = {}
    for path in ("referred", "direct"):
        group = [c for c in records if c.acquisition == path]
        matured_long = [c for c in group if c.matured(LONG_RUN_DAYS, today)]
        out[path] = {
            "customers": len(group),
            "enough": len(group) >= MIN_COHORT,
            "repeatRate": repeat_rate(group, today),
            "thirdClaimRate": third_claim_rate(group),
            "longRunRate": _pct(
                sum(1 for c in matured_long if c.still_going_after(LONG_RUN_DAYS)),
                len(matured_long)),
            "spendPerCustomer": (round(sum(c.spend for c in group) / len(group), 2)
                                 if group else 0.0),
        }
    return out


def cohorts(records: list[CustomerRecord], today: date) -> list[dict]:
    """Return rate by the month a customer first bought.

    Rows are first-claim months, columns are months elapsed. A cell is only
    filled once that whole month has passed for that cohort — an unfinished
    month is reported as immature rather than as a low number, which is the
    difference between a dashboard that tells the truth and one that appears to
    decline every time it is opened.
    """
    by_month: dict[str, list[CustomerRecord]] = {}
    for c in records:
        by_month.setdefault(c.first.strftime("%Y-%m"), []).append(c)

    rows = []
    for key in sorted(by_month):
        group = by_month[key]
        cells = []
        for n in range(1, COHORT_MONTHS + 1):
            window = n * 30
            judged = [c for c in group if c.matured(window, today)]
            matured = len(judged) == len(group) and bool(group)
            cells.append({
                "monthsAfter": n,
                "matured": matured,
                "rate": (_pct(sum(1 for c in judged if c.returned_within(window)),
                              len(judged)) if matured else 0.0),
            })
        rows.append({
            "cohort": key,
            "size": len(group),
            "enough": len(group) >= MIN_COHORT,
            "cells": cells,
        })
    return rows


# ── The referral network (Phase C) ───────────────────────────────────────────
#
# A referral edge only exists on a receipt, and routers/receipts.py will not
# accept one unless the named referrer has themselves claimed the same deal. So
# every edge sits inside one campaign, and every node in the tree below is
# somebody who actually bought. It is a tree of purchases, not of invitations.

# How deep a tree is walked before it stops. Nobody reads past this, and it is
# the belt to the cycle guard's braces.
MAX_TREE_DEPTH = 6

# Direct referrals returned per node. A single post that went well can have
# hundreds; sending them all would bloat the response for a list nobody scrolls.
MAX_TREE_CHILDREN = 25


def _children_of(records: list[CustomerRecord]) -> dict[int, list[CustomerRecord]]:
    """referrer id -> the customers they brought, best spenders first."""
    out: dict[int, list[CustomerRecord]] = {}
    for c in records:
        if c.referred_by_id is not None:
            out.setdefault(c.referred_by_id, []).append(c)
    for kids in out.values():
        kids.sort(key=lambda c: c.spend, reverse=True)
    return out


def referral_tree(records: list[CustomerRecord], root_id: int) -> Optional[dict]:
    """The chain of customers below `root_id`, as nested nodes.

    Each node carries a rollup of everyone beneath it — how many people, and
    what they spent between them. That total is the reason to open the tree at
    all: it turns "this member referred three people" into "this member is worth
    £1,240 to you", which is the figure a merchant makes decisions on.

    Cycles are real here, not theoretical. A refers B; B can then refer A back
    using a second post, because a claim is unique on (user_id, post_id) rather
    than on the pair of people. `seen` is what stops that walking forever.
    """
    by_id = {c.user_id: c for c in records}
    if root_id not in by_id:
        return None
    children = _children_of(records)

    def walk(rec: CustomerRecord, depth: int, seen: frozenset) -> dict:
        kids = []
        below_people = 0
        below_spend = 0.0
        # A node already on this path is a loop; a deeper one is past the limit.
        if depth < MAX_TREE_DEPTH:
            eligible = [k for k in children.get(rec.user_id, [])
                        if k.user_id not in seen]
            for kid in eligible[:MAX_TREE_CHILDREN]:
                node = walk(kid, depth + 1, seen | {kid.user_id})
                kids.append(node)
                below_people += 1 + node["peopleBelow"]
                below_spend += kid.spend + node["spendBelow"]
            truncated = len(eligible) > MAX_TREE_CHILDREN
        else:
            truncated = bool(children.get(rec.user_id))

        return {
            "userId": rec.user_id,
            "handle": rec.handle,
            "claims": rec.claims,
            "spend": rec.spend,
            "sticky": rec.sticky,
            "firstClaim": rec.first,
            "peopleBelow": below_people,
            "spendBelow": round(below_spend, 2),
            "truncated": truncated,
            "referred": kids,
        }

    return walk(by_id[root_id], 0, frozenset({root_id}))


def network_depth(records: list[CustomerRecord]) -> int:
    """Longest chain of referrals anywhere in this merchant's network.

    Depth of 1 means every referral came from someone who found the deal
    themselves. Depth of 2 or more means a referred customer went on to refer
    somebody else, which is the difference between one loud poster and actual
    word of mouth.
    """
    children = _children_of(records)

    def below(user_id: int, seen: frozenset) -> int:
        kids = [k for k in children.get(user_id, []) if k.user_id not in seen]
        return 1 + max((below(k.user_id, seen | {k.user_id}) for k in kids),
                       default=0) if kids else 0

    roots = [c for c in records if c.referred_by_id is None]
    return max((below(r.user_id, frozenset({r.user_id})) for r in roots), default=0)


def referrer_scores(records: list[CustomerRecord],
                    session: Session, merchant_id: int) -> list[dict]:
    """Every member who brought someone, ranked by the value of what they built.

    The column that matters is `qualityScore` — the share of their referees who
    came back. Volume is easy to game and easy to buy; five people who become
    regulars are worth more to a merchant than thirty who visit once and vanish,
    and this is the only place that distinction is visible.
    """
    by_id = {c.user_id: c for c in records}
    children = _children_of(records)

    # What each referrer's bonuses have cost this merchant. Only their own £1
    # side is counted — the referee's 50p is a different member's reward.
    bonuses: dict[int, float] = {}
    for r in session.exec(
        select(ReferralReward).where(
            ReferralReward.merchant_id == merchant_id,
            ReferralReward.kind == "referrer",
            ReferralReward.status.in_(("available", "paid")),
        )
    ).all():
        bonuses[r.user_id] = bonuses.get(r.user_id, 0.0) + r.amount

    out = []
    for user_id, kids in children.items():
        rec = by_id.get(user_id)
        if rec is None:
            continue                       # referrer never bought here themselves
        direct_spend = round(sum(k.spend for k in kids), 2)
        tree = referral_tree(records, user_id) or {}
        paid = round(bonuses.get(user_id, 0.0), 2)
        out.append({
            "userId": user_id,
            "handle": rec.handle,
            "referred": len(kids),
            "stickyReferred": sum(1 for k in kids if k.sticky),
            "qualityScore": _pct(sum(1 for k in kids if k.sticky), len(kids)),
            "referredSpend": direct_spend,
            "networkSpend": tree.get("spendBelow", direct_spend),
            "networkPeople": tree.get("peopleBelow", len(kids)),
            "bonusesPaid": paid,
            # What the merchant got back for every £1 of bonus. 0 when no bonus
            # has been paid — the deal may simply not have referrals switched on.
            "returnOnBonus": (round(tree.get("spendBelow", direct_spend) / paid, 2)
                              if paid else 0.0),
        })
    return sorted(out, key=lambda r: r["networkSpend"], reverse=True)


def network_summary(records: list[CustomerRecord], session: Session,
                    merchant_id: int) -> dict:
    """The headline numbers above the referral tree."""
    referred = [c for c in records if c.acquisition == "referred"]
    referrers = _children_of(records)
    scores = referrer_scores(records, session, merchant_id)

    rewards = session.exec(
        select(ReferralReward).where(
            ReferralReward.merchant_id == merchant_id,
            ReferralReward.status.in_(("available", "paid")),
        )
    ).all()
    spent = round(sum(r.amount for r in rewards), 2)

    return {
        # Referred customers produced per member who referred anyone. Above 1
        # and the network is compounding rather than just spreading.
        "multiplier": (round(len(referred) / len(referrers), 2)
                       if referrers else 0.0),
        "depth": network_depth(records),
        "referrers": len(referrers),
        "referredCustomers": len(referred),
        "referredSpend": round(sum(c.spend for c in referred), 2),
        "bonusesPaid": spent,
        # What a referred customer has spent for every £1 of bonus paid out.
        "returnOnBonus": (round(sum(c.spend for c in referred) / spent, 2)
                          if spent else 0.0),
        "topReferrers": scores[:10],
    }


# ── Benchmarks and incrementality (Phase E) ──────────────────────────────────
#
# Published industry reference points. These are US restaurant figures and are
# DIRECTIONAL for a UK merchant, not authoritative — the portal labels them as
# outside numbers, and they are replaced by Cirqle's own percentiles for any
# category with enough merchants to compute one honestly.
#
#   Bloom Intelligence, State of Restaurant Guest Retention, and Restroworks
#   restaurant retention statistics.
INDUSTRY = {
    "repeatRate": {"baseline": 25.0, "strong": 35.0},
    "returnRate30": {"baseline": 22.5, "strong": 40.0},
    "claimsPerCustomer": {"baseline": 1.23, "strong": 1.55},
}

# Below this many merchants in a category, a Cirqle median says more about the
# handful of brands in it than about the category, so we don't compute one.
MIN_CATEGORY_MERCHANTS = 5

# A customer counts as newly acquired if their first claim here is this recent.
NEW_WINDOW_DAYS = 90


def new_customers(records: list[CustomerRecord], today: date,
                  window: int = NEW_WINDOW_DAYS) -> list[CustomerRecord]:
    """Customers whose first claim at this merchant falls inside the window.

    Deliberately reported as "new on Cirqle", never "new to brand". We know
    somebody had not claimed here before; we cannot know whether they had been
    eating there for years before installing the app. Ibotta and Cardlytics can
    make the stronger claim because they sit on whole purchase histories through
    retailer and bank data. The weaker true claim is worth more than a strong
    one a merchant can puncture in a meeting.
    """
    return [c for c in records if (today - c.first).days <= window]


def incrementality(records: list[CustomerRecord], revenue: float,
                   programme_cost: float, today: date) -> dict:
    """A proxy for how much of this spend bought genuinely new custom.

    Not a lift study. A real one needs a control group — holding the deal back
    from a random slice of members and comparing — which needs volume Cirqle
    does not have yet, and a deliberate decision to show some members fewer
    deals. What this does instead is take the share of customers who are new and
    treat their revenue as the incremental part, which is the same shape as
    Ibotta's cost-per-incremental-dollar without the experimental backing.

    It is labelled as an estimate everywhere it surfaces, because the honest
    weakness of it is that a returning customer's spend may well have been
    incremental too, and this counts none of it.
    """
    total = len(records)
    fresh = new_customers(records, today)
    share = _pct(len(fresh), total)
    incremental_revenue = round(revenue * share / 100, 2)
    return {
        "newCustomers": len(fresh),
        "newShare": share,
        "incrementalRevenue": incremental_revenue,
        "costPerIncrementalPound": (round(programme_cost / incremental_revenue, 2)
                                    if incremental_revenue else 0.0),
        "costPerNewCustomer": (round(programme_cost / len(fresh), 2)
                               if fresh else 0.0),
    }


def summary(records: list[CustomerRecord], session: Session,
            merchant_id: int, today: Optional[date] = None) -> dict:
    """Every headline number the Customers tab shows above the table."""
    today = today or date.today()
    total = len(records)
    revenue = round(sum(c.spend for c in records), 2)
    claims = sum(c.claims for c in records)

    rewards = session.exec(
        select(ReferralReward).where(
            ReferralReward.merchant_id == merchant_id,
            ReferralReward.status.in_(("available", "paid")),
        )
    ).all()

    return {
        "customers": total,
        "sticky": sum(1 for c in records if c.sticky),
        "repeatRate": repeat_rate(records, today),
        "thirdClaimRate": third_claim_rate(records),
        "returnRate30": return_rate(records, 30, today),
        "returnRate60": return_rate(records, 60, today),
        "returnRate90": return_rate(records, 90, today),
        "medianDaysToSecond": median_days_to_second(records),
        "claimsPerCustomer": round(claims / total, 2) if total else 0.0,
        "revenuePerCustomer": round(revenue / total, 2) if total else 0.0,
        "lapsed": len(lapsed(records, today)),
        "referredCustomers": sum(1 for c in records if c.acquisition == "referred"),
        "referralBonusesPaid": round(sum(r.amount for r in rewards), 2),
    }
