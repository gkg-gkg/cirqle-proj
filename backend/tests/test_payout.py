"""Tests for compute_payout (spec cashback-algorithm v1.0 §4-5) and the
budget-exhaustion path in receipts._apply_shadow_scoring."""
from datetime import datetime

import pytest

from app.aqs import compute_payout
from app.models import Campaign, Mention, Receipt, User
from app.routers.receipts import _apply_shadow_scoring


def _flat_campaign(**overrides) -> Campaign:
    defaults = dict(brand="Acme", title="Acme deal", earn="£13.00", cashback_mode="flat")
    defaults.update(overrides)
    return Campaign(**defaults)


def _performance_campaign(**overrides) -> Campaign:
    defaults = dict(
        brand="Acme", title="Acme deal", earn="£13.00", cashback_mode="performance",
        base_cashback=20.0, expected_engagement_baseline=50, max_multiplier=3.0,
    )
    defaults.update(overrides)
    return Campaign(**defaults)


def test_flat_mode_ignores_aqs_and_engagement():
    c = _flat_campaign()
    result = compute_payout(c, aqs_score=0.5, likes=0, comments=0)
    assert result.payout == pytest.approx(13.0)


def test_performance_floor_is_25_percent_of_base():
    c = _performance_campaign()
    # aqs floor 0.5 * engagement floor 0.5 = 25% of base_cashback
    result = compute_payout(c, aqs_score=0.5, likes=0, comments=0)
    assert result.engagement_multiplier == pytest.approx(0.5)
    assert result.payout == pytest.approx(20.0 * 0.5 * 0.5)


def test_performance_ceiling_clamps_engagement_multiplier():
    c = _performance_campaign()
    # likes+comments far above baseline*max_multiplier -> clamped to max_multiplier
    result = compute_payout(c, aqs_score=1.0, likes=10_000, comments=0)
    assert result.engagement_multiplier == pytest.approx(3.0)
    assert result.payout == pytest.approx(20.0 * 1.0 * 3.0)


def test_per_post_cap_limits_raw_payout():
    c = _performance_campaign(per_post_cap=5.0)
    result = compute_payout(c, aqs_score=1.0, likes=10_000, comments=0)
    assert result.payout == pytest.approx(5.0)


def test_default_baseline_and_ceiling_used_when_campaign_has_none():
    c = _performance_campaign(expected_engagement_baseline=None, max_multiplier=None)
    # DEFAULT_ENGAGEMENT_BASELINE=50, so 50 likes+comments -> multiplier 1.0
    result = compute_payout(c, aqs_score=1.0, likes=50, comments=0)
    assert result.engagement_multiplier == pytest.approx(1.0)
    assert result.payout == pytest.approx(20.0)


def test_budget_exhaustion_flags_pending_review_without_deducting(session):
    user = User(first_name="A", last_name="B", email="a@b.com", password_hash="x")
    session.add(user)
    session.commit()
    session.refresh(user)

    campaign = _performance_campaign(budget_total=10.0, budget_remaining=10.0)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    mention = Mention(id="postA", user_id=user.id, likes_count=10_000, comments_count=0,
                      follower_count_at_scrape=1000, following_count_at_scrape=100,
                      scraped_at=datetime.utcnow())
    session.add(mention)

    receipt = Receipt(user_id=user.id, post_id="postA", campaign_id=campaign.id,
                      brand="Acme", amount=13.0, image_key="k")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    _apply_shadow_scoring(receipt, session)

    assert receipt.status == "pending_budget_review"
    assert receipt.amount == pytest.approx(13.0)          # untouched
    assert campaign.budget_remaining == pytest.approx(10.0)  # untouched
    assert receipt.shadow_payout is not None


def test_budget_sufficient_deducts_and_verifies(session):
    user = User(first_name="A", last_name="B", email="a@b.com", password_hash="x")
    session.add(user)
    session.commit()
    session.refresh(user)

    campaign = _performance_campaign(budget_total=100.0, budget_remaining=100.0)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    mention = Mention(id="postB", user_id=user.id, likes_count=50, comments_count=0,
                      follower_count_at_scrape=1000, following_count_at_scrape=100,
                      scraped_at=datetime.utcnow())
    session.add(mention)

    receipt = Receipt(user_id=user.id, post_id="postB", campaign_id=campaign.id,
                      brand="Acme", amount=13.0, image_key="k")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    _apply_shadow_scoring(receipt, session)

    assert receipt.status == "verified"
    assert campaign.budget_remaining < 100.0


def test_flat_campaign_shadow_scores_without_touching_amount_or_budget(session):
    user = User(first_name="A", last_name="B", email="a@b.com", password_hash="x")
    session.add(user)
    session.commit()
    session.refresh(user)

    campaign = _flat_campaign()
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    mention = Mention(id="postC", user_id=user.id, likes_count=50, comments_count=0,
                      follower_count_at_scrape=1000, following_count_at_scrape=100,
                      scraped_at=datetime.utcnow())
    session.add(mention)

    receipt = Receipt(user_id=user.id, post_id="postC", campaign_id=campaign.id,
                      brand="Acme", amount=13.0, image_key="k")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    _apply_shadow_scoring(receipt, session)

    assert receipt.status == "verified"
    assert receipt.amount == pytest.approx(13.0)      # real payout untouched
    assert receipt.shadow_payout is not None          # but shadow data was still logged
    assert receipt.algorithm_version is not None
