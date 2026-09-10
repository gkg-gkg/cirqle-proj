"""The national referral competition: seasons, streaks, ranking and freezing.

A referral is dated by when the referred claim CLEARED, not by when its
ReferralReward row was written (see app/leaderboard.py). These tests build that
moment directly: with no Mention behind a claim, cashback.clears_at() falls back
to the upload date, so a receipt uploaded CONFIRM_DAYS before a target date
clears exactly on it.
"""
from datetime import date, datetime, timedelta

import pytest

from app import leaderboard
from app.cashback import CONFIRM_DAYS
from app.models import Receipt, ReferralReward, SeasonStanding, User
from app.security import create_token, hash_password


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_user(session, handle, opted_in=True, display_name=""):
    u = User(first_name="Test", last_name="Member",
             email=f"{handle}@example.com",
             password_hash=hash_password("correcthorse7"),
             instagram_handle=handle, status="approved",
             leaderboard_opt_in=opted_in, display_name=display_name)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def refer(session, user, clears_on, kind="referrer", status="available"):
    """Record one referral by `user` that clears at `clears_on` (a datetime)."""
    receipt = Receipt(
        user_id=user.id, post_id="", campaign_id=None, brand="Nike",
        amount=10.0, image_key=f"receipts/{user.id}-{clears_on.timestamp()}.jpg",
        status="verified",
        uploaded_at=clears_on - timedelta(days=CONFIRM_DAYS),
    )
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    reward = ReferralReward(user_id=user.id, kind=kind, receipt_id=receipt.id,
                            amount=1.0, status=status)
    session.add(reward)
    session.commit()
    return reward


def auth(user):
    return {"Authorization": f"Bearer {create_token(user)}"}


def times_for(session, user):
    return leaderboard.clearing_times(session, user.id).get(user.id, [])


# ── Seasons ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("day,label", [
    (date(2026, 1, 1), "2026-Q1"),
    (date(2026, 3, 31), "2026-Q1"),
    (date(2026, 4, 1), "2026-Q2"),
    (date(2026, 9, 10), "2026-Q3"),
    (date(2026, 12, 31), "2026-Q4"),
])
def test_season_of(day, label):
    assert leaderboard.season_of(day) == label


def test_season_bounds_cover_the_whole_quarter():
    assert leaderboard.season_bounds("2026-Q3") == (date(2026, 7, 1), date(2026, 9, 30))
    # Q4 has to roll into the next year to find its last day.
    assert leaderboard.season_bounds("2026-Q4") == (date(2026, 10, 1), date(2026, 12, 31))


def test_season_freezes_after_claims_have_had_time_to_clear():
    assert leaderboard.freezes_on("2026-Q3") == date(2026, 10, 8)
    assert not leaderboard.is_final("2026-Q3", date(2026, 10, 7))
    assert leaderboard.is_final("2026-Q3", date(2026, 10, 8))


# ── Streaks ──────────────────────────────────────────────────────────────────

def test_no_referrals_is_no_streak(session):
    alice = make_user(session, "alice")
    assert leaderboard.streak_weeks(times_for(session, alice), date(2026, 9, 10)) == 0


def test_consecutive_weeks_build_a_streak(session):
    alice = make_user(session, "alice")
    # Three Wednesdays running, ending in the current week.
    for day in (date(2026, 8, 26), date(2026, 9, 2), date(2026, 9, 9)):
        refer(session, alice, datetime.combine(day, datetime.min.time()))
    assert leaderboard.streak_weeks(times_for(session, alice), date(2026, 9, 10)) == 3


def test_two_referrals_in_one_week_count_once(session):
    alice = make_user(session, "alice")
    refer(session, alice, datetime(2026, 9, 8, 10))
    refer(session, alice, datetime(2026, 9, 9, 10))
    assert leaderboard.streak_weeks(times_for(session, alice), date(2026, 9, 10)) == 1


def test_quiet_current_week_does_not_break_the_streak(session):
    """The week in progress can still be saved, so it never ends a streak."""
    alice = make_user(session, "alice")
    refer(session, alice, datetime(2026, 9, 2, 12))     # last week only
    # Thursday of the current week, nothing in it yet.
    assert leaderboard.streak_weeks(times_for(session, alice), date(2026, 9, 10)) == 1


def test_empty_week_breaks_the_streak_only_once_it_has_settled(session):
    """The heart of the grace period.

    The week of 31 Aug is empty. A referral earned in it would clear three days
    later, so until then we cannot tell an empty week from an unsettled one —
    and breaking the streak early would punish a member who had kept it.
    """
    alice = make_user(session, "alice")
    refer(session, alice, datetime(2026, 8, 26, 12))    # week of 24 Aug
    refer(session, alice, datetime(2026, 9, 8, 12))     # week of 7 Sep

    week_of_31_aug = date(2026, 8, 31)
    settles_on = week_of_31_aug + timedelta(days=7 + leaderboard.GRACE_DAYS)
    assert settles_on == date(2026, 9, 10)

    times = times_for(session, alice)
    # Day before it settles: the gap is stepped over, so both active weeks count.
    assert leaderboard.streak_weeks(times, date(2026, 9, 9)) == 2
    # The day it settles: the gap is now real and ends the walk.
    assert leaderboard.streak_weeks(times, settles_on) == 1


# ── Scores ───────────────────────────────────────────────────────────────────

def test_score_counts_only_referrals_inside_the_season(session):
    alice = make_user(session, "alice")
    refer(session, alice, datetime(2026, 6, 30, 23))    # Q2, one day out
    refer(session, alice, datetime(2026, 7, 1, 1))      # Q3, first day
    refer(session, alice, datetime(2026, 9, 30, 23))    # Q3, last day
    refer(session, alice, datetime(2026, 10, 1, 1))     # Q4, one day out

    times = times_for(session, alice)
    assert leaderboard.score_for(times, "2026-Q3") == 2
    assert leaderboard.score_for(times, "2026-Q2") == 1
    assert leaderboard.score_for(times, "2026-Q4") == 1


def test_being_referred_earns_no_points(session):
    """The referee's own 50p is a different kind of row and must not score."""
    bob = make_user(session, "bob")
    refer(session, bob, datetime(2026, 9, 1, 12), kind="referee")
    assert leaderboard.score_for(times_for(session, bob), "2026-Q3") == 0


def test_cancelled_referral_earns_no_points(session):
    alice = make_user(session, "alice")
    refer(session, alice, datetime(2026, 9, 1, 12), status="cancelled")
    assert leaderboard.score_for(times_for(session, alice), "2026-Q3") == 0


# ── Ranking ──────────────────────────────────────────────────────────────────

def test_opted_out_member_is_absent_from_the_table(session):
    shown = make_user(session, "shown", opted_in=True)
    hidden = make_user(session, "hidden", opted_in=False)
    refer(session, shown, datetime(2026, 9, 1, 12))
    refer(session, hidden, datetime(2026, 9, 1, 12))
    refer(session, hidden, datetime(2026, 9, 2, 12))    # more referrals, still absent

    rows, _ = leaderboard.standings(session, "2026-Q3", today=date(2026, 9, 10))
    assert [r["userId"] for r in rows] == [shown.id]


def test_tie_is_broken_by_who_got_there_first(session):
    alice = make_user(session, "alice")
    bob = make_user(session, "bob")
    # Both finish on two referrals; alice's second lands a day earlier.
    refer(session, alice, datetime(2026, 8, 1, 12))
    refer(session, alice, datetime(2026, 9, 1, 12))
    refer(session, bob, datetime(2026, 7, 1, 12))
    refer(session, bob, datetime(2026, 9, 2, 12))

    rows, _ = leaderboard.standings(session, "2026-Q3", today=date(2026, 9, 10))
    assert [r["userId"] for r in rows] == [alice.id, bob.id]
    assert [r["rank"] for r in rows] == [1, 2]


def test_no_referrals_never_wins_a_prize():
    assert leaderboard.wins_prize(rank=1, score=1)
    assert leaderboard.wins_prize(rank=10, score=3)
    assert not leaderboard.wins_prize(rank=11, score=99)   # past the ten places
    assert not leaderboard.wins_prize(rank=4, score=0)     # nothing to reward


def test_display_name_falls_back_to_the_handle(session):
    named = make_user(session, "alice", display_name="Alice C")
    plain = make_user(session, "bob")
    assert leaderboard.display_name(named) == "Alice C"
    assert leaderboard.display_name(plain) == "@bob"


# ── Freezing ─────────────────────────────────────────────────────────────────

def test_a_running_season_is_not_final_and_stores_nothing(session):
    alice = make_user(session, "alice")
    refer(session, alice, datetime(2026, 9, 1, 12))

    rows, final = leaderboard.standings(session, "2026-Q3", today=date(2026, 9, 10))
    assert final is False
    assert len(rows) == 1
    assert session.exec(SeasonStanding.__table__.select()).first() is None


def test_a_finished_season_freezes_and_then_ignores_late_referrals(session):
    """The reason SeasonStanding exists.

    Once a season's table is final it is what a prize was decided on. A referral
    that clears afterwards — a claim approved late, a bonus settled on someone's
    first login in weeks — must not reorder it.
    """
    alice = make_user(session, "alice")
    bob = make_user(session, "bob")
    refer(session, alice, datetime(2026, 2, 1, 12))
    refer(session, alice, datetime(2026, 2, 8, 12))
    refer(session, bob, datetime(2026, 2, 2, 12))

    after_freeze = date(2026, 4, 8)
    first, final = leaderboard.standings(session, "2026-Q1", today=after_freeze)
    assert final is True
    assert [(r["userId"], r["rank"], r["score"]) for r in first] == [
        (alice.id, 1, 2), (bob.id, 2, 1)]

    # A third member's referral lands inside Q1, long after it froze.
    carol = make_user(session, "carol")
    for day in (3, 4, 5, 6):
        refer(session, carol, datetime(2026, 2, day, 12))

    again, final = leaderboard.standings(session, "2026-Q1", today=after_freeze)
    assert final is True
    assert [(r["userId"], r["rank"], r["score"]) for r in again] == [
        (alice.id, 1, 2), (bob.id, 2, 1)]
    assert carol.id not in [r["userId"] for r in again]


def test_freezing_survives_being_reached_twice(session):
    """Two requests can arrive at the freeze date together."""
    alice = make_user(session, "alice")
    refer(session, alice, datetime(2026, 2, 1, 12))

    leaderboard.standings(session, "2026-Q1", today=date(2026, 4, 8))
    rows, _ = leaderboard.standings(session, "2026-Q1", today=date(2026, 4, 8))

    stored = session.exec(SeasonStanding.__table__.select()).all()
    assert len(stored) == 1
    assert len(rows) == 1


# ── The endpoint ─────────────────────────────────────────────────────────────

def test_endpoint_shows_your_own_rank_when_you_are_off_the_table(session, client,
                                                                 monkeypatch):
    monkeypatch.setattr("app.routers.leaderboard.TOP_N", 2)
    winners = [make_user(session, f"winner{i}") for i in range(3)]
    for i, user in enumerate(winners):
        for n in range(3 - i):                       # 3, 2, then 1 referral
            refer(session, user, datetime(2026, 9, 1 + n, 12))
    last = winners[-1]

    body = client.get("/leaderboard?season=2026-Q3", headers=auth(last)).json()
    assert len(body["top"]) == 2
    assert last.id not in [row["userId"] for row in body["top"]]
    assert body["you"]["rank"] == 3
    assert body["you"]["isYou"] is True
    assert body["entrants"] == 3


def test_endpoint_rejects_a_season_that_is_not_one(session, client):
    alice = make_user(session, "alice")
    reply = client.get("/leaderboard?season=last-year", headers=auth(alice))
    assert reply.status_code == 422


def test_endpoint_marks_the_paying_places(session, client):
    alice = make_user(session, "alice")
    refer(session, alice, datetime(2026, 9, 1, 12))
    body = client.get("/leaderboard?season=2026-Q3", headers=auth(alice)).json()
    assert body["top"][0]["winsPrize"] is True
    assert body["prizeAmount"] == 10.0
    assert body["prizePlaces"] == 10
    assert body["isFinal"] is False


def test_opt_in_toggle_puts_you_on_and_off_the_table(session, client):
    alice = make_user(session, "alice", opted_in=False)
    refer(session, alice, datetime(2026, 9, 1, 12))

    body = client.get("/leaderboard?season=2026-Q3", headers=auth(alice)).json()
    assert body["entrants"] == 0
    assert body["optedIn"] is False

    stats = client.post("/account/leaderboard-opt-in",
                        json={"optIn": True, "displayName": "Alice C"},
                        headers=auth(alice)).json()
    assert stats["leaderboardOptIn"] is True
    assert stats["seasonScore"] == 1

    body = client.get("/leaderboard?season=2026-Q3", headers=auth(alice)).json()
    assert body["entrants"] == 1
    assert body["top"][0]["name"] == "Alice C"


def test_dashboard_reports_streak_and_season_score(session, client):
    alice = make_user(session, "alice")
    refer(session, alice, datetime(2026, 9, 1, 12))
    refer(session, alice, datetime(2026, 9, 8, 12))

    stats = client.get("/account/stats", headers=auth(alice)).json()
    assert stats["season"] == leaderboard.current_season()
    assert stats["seasonScore"] == 2
    assert stats["referralStreak"] >= 1
