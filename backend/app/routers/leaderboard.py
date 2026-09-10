"""The national referral leaderboard (Phase 10).

One table per season, ranked by how many people a member has referred whose
purchase actually cleared. The rules and the reasoning live in
app/leaderboard.py; this file is just the way in.

Signed-in members only. Not because the standings are secret — everyone on them
chose to appear — but because every response is personalised: it has to say
which row is yours, and show your own rank when you are nowhere near the top.
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from .. import leaderboard
from ..db import get_session
from ..models import LeaderboardEntry, LeaderboardOut, User
from ..security import get_current_user

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])

# How many rows the table sends. Your own row is sent separately, so being past
# this cut-off still tells you where you stand.
TOP_N = 100

_SEASON_RE = re.compile(r"^\d{4}-Q[1-4]$")


def _entry(row: dict, user_id: int) -> LeaderboardEntry:
    return LeaderboardEntry(
        rank=row["rank"],
        userId=row["userId"],
        name=row["name"],
        score=row["score"],
        isYou=row["userId"] == user_id,
        winsPrize=leaderboard.wins_prize(row["rank"], row["score"]),
    )


@router.get("", response_model=LeaderboardOut)
def get_leaderboard(
    season: str = Query(default="", description="e.g. 2026-Q3; defaults to the current one"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The season's table, plus where this member sits in it."""
    season = season or leaderboard.current_season()
    if not _SEASON_RE.match(season):
        raise HTTPException(
            status_code=422,
            detail="A season looks like 2026-Q3 — a year, then Q1 to Q4.")

    rows, final = leaderboard.standings(session, season)
    starts_on, ends_on = leaderboard.season_bounds(season)

    mine = next((r for r in rows if r["userId"] == user.id), None)
    return LeaderboardOut(
        season=season,
        startsOn=starts_on,
        endsOn=ends_on,
        freezesOn=leaderboard.freezes_on(season),
        isFinal=final,
        prizeAmount=leaderboard.PRIZE_AMOUNT,
        prizePlaces=leaderboard.PRIZE_PLACES,
        entrants=len(rows),
        top=[_entry(r, user.id) for r in rows[:TOP_N]],
        you=_entry(mine, user.id) if mine else None,
        optedIn=user.leaderboard_opt_in,
    )
