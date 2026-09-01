"""Instagram feed endpoints (Phase 2).

- `POST /feed/refresh` scrapes the brand's Instagram mentions server-side (using
  the server's Apify token), keeps only the signed-in user's posts, **saves them
  to the database** keyed by user, and returns them.
- `GET /feed` returns that user's stored posts (no scrape) — used on page load so
  each user's list is durable and follows their account, not just their browser.

The browser never sees the Apify token, and each user only sees their own posts.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..db import get_session
from ..handles import normalize_handle
from ..instagram import ScrapeError, scrape_brand_mentions, scrape_profile_stats
from ..models import FeedPost, FeedRefreshOut, Mention, User
from ..security import get_current_user

router = APIRouter(prefix="/feed", tags=["feed"])


def _user_handle(user: User) -> str:
    """This user's normalised Instagram handle, or '' if none set."""
    return normalize_handle(user.instagram_handle or "")


def _mention_to_post(m: Mention) -> FeedPost:
    """A stored row -> the shape the feed page renders."""
    return FeedPost(
        id=m.id,
        url=m.url,
        displayUrl=m.display_url,
        caption=m.caption,
        timestamp=m.timestamp,
        ownerUsername=m.owner_username,
        ownerFullName=m.owner_full_name,
        likesCount=m.likes_count,
        commentsCount=m.comments_count,
    )


@router.get("", response_model=FeedRefreshOut)
def get_feed(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Return this user's stored posts (no scrape)."""
    rows = session.exec(
        select(Mention).where(Mention.user_id == user.id)
    ).all()
    updated = max((m.scraped_at for m in rows), default=None)
    return FeedRefreshOut(posts=[_mention_to_post(m) for m in rows], updated=updated)


@router.post("/refresh", response_model=FeedRefreshOut)
def refresh(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Scrape brand mentions, save this user's posts, and return them."""
    handle = _user_handle(user)
    if not handle:
        raise HTTPException(
            status_code=400,
            detail="No Instagram handle on your account. Add one to see your posts.",
        )

    try:
        raw_posts = scrape_brand_mentions(limit=50)
    except ScrapeError as exc:
        # 503: the scrape (an upstream dependency) is unavailable/misconfigured.
        raise HTTPException(status_code=503, detail=str(exc))

    mine = [p for p in raw_posts if (p.get("ownerUsername") or "").lower() == handle]
    now = datetime.now(timezone.utc)
    # Best-effort — never blocks the refresh if it fails (see scrape_profile_stats).
    profile_stats = scrape_profile_stats(handle)

    # Replace this user's stored set with the fresh scrape (delete-then-insert,
    # flushed in between so re-using the same post id doesn't clash).
    for old in session.exec(select(Mention).where(Mention.user_id == user.id)).all():
        session.delete(old)
    session.flush()

    posts: list[FeedPost] = []
    for p in mine:
        post_id = p.get("id")
        fp = FeedPost(
            id=post_id,
            url=p.get("url"),
            displayUrl=p.get("displayUrl"),
            caption=p.get("caption"),
            timestamp=p.get("timestamp"),
            ownerUsername=p.get("ownerUsername"),
            ownerFullName=p.get("ownerFullName"),
            likesCount=p.get("likesCount"),
            commentsCount=p.get("commentsCount"),
        )
        posts.append(fp)
        if post_id:  # need an id to store it (it's the primary key)
            session.add(Mention(
                id=post_id,
                user_id=user.id,
                url=fp.url,
                display_url=fp.displayUrl,
                caption=fp.caption,
                timestamp=fp.timestamp,
                owner_username=fp.ownerUsername,
                owner_full_name=fp.ownerFullName,
                likes_count=fp.likesCount,
                comments_count=fp.commentsCount,
                follower_count_at_scrape=profile_stats.get("followers") if profile_stats else None,
                following_count_at_scrape=profile_stats.get("following") if profile_stats else None,
                scraped_at=now,
            ))
    session.commit()

    return FeedRefreshOut(posts=posts, updated=now)
