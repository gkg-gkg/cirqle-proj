"""Account Quality Score (AQS) — Cirqle's performance-cashback algorithm v1.0.

This is the single source of truth for the constants and formulas in the
locked cashback-algorithm spec. It is intentionally independent of
`cashback.py` (which owns the unrelated concern of *when* a claim's cashback
clears relative to the post date).

Two deliberate deviations from the spec's literal wording, both documented
here rather than silently baked in:

  1. "Computed ... at admin-approval time" means the actual moment
     `verify_receipt`/`bulk_verify_receipts` runs, not necessarily the exact
     end of the 3-day window — there's no background-job infrastructure in
     this codebase to defer computation to a specific instant, and the
     receipt-approval endpoints already only allow approval *before* the
     window closes.
  2. The maturity sub-score's "account_created_at unknown" branch is not a
     rare fallback here — no current scrape source exposes Instagram account
     creation date, so `maturity_score(None)` is the only input this codebase
     can produce today. It's still a real, useful signal once/if that data
     becomes available, so the function keeps the full signature.
"""
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .models import Mention, User

AQS_FLOOR = 0.5
AQS_CEILING = 1.0
AQS_WEIGHTS = {
    "ratio": 0.30,
    "engagement_rate": 0.35,
    "maturity": 0.15,
    "consistency": 0.20,
}

ENGAGEMENT_MULTIPLIER_FLOOR = 0.5
ENGAGEMENT_MULTIPLIER_CEILING_DEFAULT = 3.0   # per-campaign override allowed

DEFAULT_ENGAGEMENT_BASELINE = 50   # combined likes+comments; per-campaign override allowed
ALGORITHM_VERSION = "v1.0"

# Sub-scores fall back to this whenever their required input data is missing
# (no scrape yet, scrape failed, or — for maturity — no data source exists at
# all). Matches the spec's own treatment of maturity's "unknown" case.
_NEUTRAL_SCORE = 0.8


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def ratio_score(followers: Optional[int], following: Optional[int]) -> float:
    """followers / max(following, 1). >=1.0 -> 1.0; 0.3-1.0 -> linear 0.6-1.0; <0.3 -> 0.4."""
    if followers is None or following is None:
        return _NEUTRAL_SCORE
    ratio = followers / max(following, 1)
    if ratio >= 1.0:
        return 1.0
    if ratio >= 0.3:
        return 0.6 + (ratio - 0.3) / (1.0 - 0.3) * (1.0 - 0.6)
    return 0.4


def engagement_rate_score(likes: int, comments: int, followers: Optional[int]) -> float:
    """(likes+comments)/followers. <0.3% -> 0.4; 0.3-1% -> linear 0.6-1.0;
    1-12% -> 1.0; 12-25% -> linear 1.0-0.7; >25% -> 0.4."""
    if followers is None or followers <= 0:
        return _NEUTRAL_SCORE
    rate = (likes + comments) / followers
    if rate < 0.003:
        return 0.4
    if rate < 0.01:
        return 0.6 + (rate - 0.003) / (0.01 - 0.003) * (1.0 - 0.6)
    if rate <= 0.12:
        return 1.0
    if rate <= 0.25:
        return 1.0 - (rate - 0.12) / (0.25 - 0.12) * (1.0 - 0.7)
    return 0.4


def maturity_score(account_created_at: Optional[datetime]) -> float:
    """Unknown -> 0.8 (neutral — the common case today, see module docstring).
    <30 days -> 0.5; 30-180 days -> 0.8; >180 days -> 1.0."""
    if account_created_at is None:
        return _NEUTRAL_SCORE
    age_days = (datetime.utcnow() - account_created_at).days
    if age_days < 30:
        return 0.5
    if age_days <= 180:
        return 0.8
    return 1.0


def consistency_score(recent_rates: list[float]) -> float:
    """stddev/mean of engagement rate across up to the last 5 tagged posts.
    <3 usable posts -> 0.8 (neutral, insufficient history). stddev/mean<0.15
    AND mean>5% (suspiciously uniform *and* high) -> 0.4. Otherwise 1.0."""
    if len(recent_rates) < 3:
        return _NEUTRAL_SCORE
    mean = statistics.mean(recent_rates)
    if mean <= 0:
        return 1.0
    coefficient_of_variation = statistics.pstdev(recent_rates) / mean
    if coefficient_of_variation < 0.15 and mean > 0.05:
        return 0.4
    return 1.0


@dataclass
class AqsResult:
    score: float
    inputs_snapshot: dict = field(default_factory=dict)


def compute_aqs(user: User, mentions: list[Mention]) -> AqsResult:
    """Lazily computed from the most recent scrape data on file for this user.

    Ratio/engagement-rate use the single most-recently-scraped Mention's
    follower/following/likes/comments snapshot. Consistency uses up to the 5
    most recent Mentions that carry a usable engagement-rate (likes/comments
    AND a follower snapshot present).
    """
    ordered = sorted(mentions, key=lambda m: m.scraped_at, reverse=True)
    latest = ordered[0] if ordered else None

    followers = latest.follower_count_at_scrape if latest else None
    following = latest.following_count_at_scrape if latest else None
    likes = (latest.likes_count or 0) if latest else 0
    comments = (latest.comments_count or 0) if latest else 0

    usable_rates: list[float] = []
    for m in ordered:
        if m.follower_count_at_scrape and m.follower_count_at_scrape > 0:
            usable_rates.append(((m.likes_count or 0) + (m.comments_count or 0)) / m.follower_count_at_scrape)
        if len(usable_rates) >= 5:
            break

    r_score = ratio_score(followers, following)
    e_score = engagement_rate_score(likes, comments, followers)
    m_score = maturity_score(None)   # no account-creation-date source exists today
    c_score = consistency_score(usable_rates)

    raw_aqs = (
        AQS_WEIGHTS["ratio"] * r_score
        + AQS_WEIGHTS["engagement_rate"] * e_score
        + AQS_WEIGHTS["maturity"] * m_score
        + AQS_WEIGHTS["consistency"] * c_score
    )
    aqs_score = _clamp(raw_aqs, AQS_FLOOR, AQS_CEILING)

    return AqsResult(
        score=aqs_score,
        inputs_snapshot={
            "ratio_score": r_score,
            "engagement_rate_score": e_score,
            "maturity_score": m_score,
            "consistency_score": c_score,
            "followers": followers,
            "following": following,
            "likes": likes,
            "comments": comments,
            "consistency_sample_size": len(usable_rates),
        },
    )
