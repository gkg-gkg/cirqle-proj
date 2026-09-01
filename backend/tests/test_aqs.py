"""Boundary tests for the AQS sub-scores (spec cashback-algorithm v1.0 §3).

Each table is deliberately exhaustive at the exact band edges called out in
the spec, since off-by-one boundary handling is exactly what's easy to get
wrong (e.g. whether the top of one band or the bottom of the next "owns" the
boundary value).
"""
from datetime import datetime, timedelta

import pytest

from app.aqs import (compute_aqs, consistency_score, engagement_rate_score,
                     maturity_score, ratio_score)
from app.models import Mention, User


@pytest.mark.parametrize(
    "followers,following,expected",
    [
        (None, 100, 0.8),          # missing data -> neutral
        (100, None, 0.8),          # missing data -> neutral
        (100, 100, 1.0),           # ratio == 1.0 -> ceiling
        (200, 100, 1.0),           # ratio > 1.0 -> ceiling
        (30, 100, 0.6),            # ratio == 0.3 -> bottom of linear band
        (65, 100, 0.8),            # ratio == 0.65 -> midpoint of linear band
        (29, 100, 0.4),            # ratio just under 0.3 -> floor
        (0, 100, 0.4),             # ratio == 0 -> floor
    ],
)
def test_ratio_score_boundaries(followers, following, expected):
    assert ratio_score(followers, following) == pytest.approx(expected)


@pytest.mark.parametrize(
    "likes,comments,followers,expected",
    [
        (0, 0, None, 0.8),         # missing data -> neutral
        (0, 0, 0, 0.8),            # zero followers -> neutral (avoid div-by-zero)
        (0, 2, 1000, 0.4),         # 0.2% -> below floor band
        (3, 0, 1000, 0.6),         # 0.3% exactly -> bottom of first linear band
        (65, 0, 10000, 0.8),       # 0.65% -> midpoint of first linear band
        (10, 0, 1000, 1.0),        # 1% exactly -> normal band
        (60, 0, 1000, 1.0),        # 6% -> middle of normal band
        (120, 0, 1000, 1.0),       # 12% exactly -> top of normal band
        (185, 0, 1000, 0.85),      # 18.5% -> midpoint of second linear band
        (250, 0, 1000, 0.7),       # 25% exactly -> bottom of second linear band
        (260, 0, 1000, 0.4),       # 26% -> above ceiling, bot/pod territory
    ],
)
def test_engagement_rate_score_boundaries(likes, comments, followers, expected):
    assert engagement_rate_score(likes, comments, followers) == pytest.approx(expected)


def test_engagement_rate_score_below_floor_exact():
    # 0.29% (just under the 0.3% floor) -> 0.4
    assert engagement_rate_score(29, 0, 10000) == pytest.approx(0.4)


@pytest.mark.parametrize(
    "age_days,expected",
    [
        (None, 0.8),     # unknown -> neutral
        (10, 0.5),       # < 30 days
        (29, 0.5),       # just under 30 days
        (30, 0.8),       # == 30 days -> matured band
        (180, 0.8),      # == 180 days -> still matured band
        (181, 1.0),      # > 180 days
        (400, 1.0),
    ],
)
def test_maturity_score_boundaries(age_days, expected):
    created_at = None if age_days is None else datetime.utcnow() - timedelta(days=age_days)
    assert maturity_score(created_at) == pytest.approx(expected)


@pytest.mark.parametrize(
    "rates,expected",
    [
        ([], 0.8),                          # 0 posts -> neutral
        ([0.05], 0.8),                      # 1 post -> neutral
        ([0.05, 0.06], 0.8),                # 2 posts -> neutral
        ([0.06, 0.06, 0.06], 0.4),           # uniform AND high (mean > 5%) -> suspicious
        ([0.02, 0.02, 0.02], 1.0),           # uniform but low mean (<= 5%) -> not suspicious
        ([0.01, 0.15, 0.03], 1.0),           # high variance -> normal
    ],
)
def test_consistency_score_boundaries(rates, expected):
    assert consistency_score(rates) == pytest.approx(expected)


def test_compute_aqs_no_data_returns_neutral_weighted_average():
    user = User(first_name="A", last_name="B", email="a@b.com", password_hash="x")
    result = compute_aqs(user, [])
    # every sub-score is neutral (0.8) when there's no scrape data at all, so
    # the weighted average (weights sum to 1) is also 0.8.
    assert result.score == pytest.approx(0.8)
    assert result.inputs_snapshot["followers"] is None


def test_compute_aqs_uses_most_recent_mention_for_ratio_and_engagement():
    user = User(first_name="A", last_name="B", email="a@b.com", password_hash="x")
    older = Mention(
        id="p1", user_id=1, likes_count=1000, comments_count=0,
        follower_count_at_scrape=100, following_count_at_scrape=100,
        scraped_at=datetime.utcnow() - timedelta(days=5),
    )
    newer = Mention(
        id="p2", user_id=1, likes_count=10, comments_count=0,
        follower_count_at_scrape=1000, following_count_at_scrape=100,
        scraped_at=datetime.utcnow(),
    )
    result = compute_aqs(user, [older, newer])
    assert result.inputs_snapshot["followers"] == 1000
    assert result.inputs_snapshot["likes"] == 10
