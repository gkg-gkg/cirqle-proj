"""Instagram feed endpoints (Phase 2).

- `POST /feed/refresh` scrapes the brand's Instagram mentions server-side (using
  the server's Apify token), keeps only the signed-in user's posts, **saves them
  to the database** keyed by user, and returns them.
- `GET /feed` returns that user's stored posts (no scrape) — used on page load so
  each user's list is durable and follows their account, not just their browser.

The browser never sees the Apify token, and each user only sees their own posts.
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import brandtags
from ..db import get_session
from ..handles import normalize_handle
from ..instagram import (
    ScrapeError,
    extract_tagged_handles,
    mirror_display_image,
    scrape_brand_mentions,
    scrape_profile_stats,
)
from ..models import (FeedPost, FeedRefreshOut, Mention, PostCampaignsOut,
                      User)
from ..ratelimit import rate_limit
from ..security import get_current_user
from .campaigns import _campaign_out

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
        taggedHandles=brandtags.decode_handles(m) or [],
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


@router.get("/posts/{post_id}/campaigns", response_model=PostCampaignsOut)
def post_campaigns(
    post_id: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The deals this post is allowed to claim — the brands it actually tags.

    The receipt page fills its deal picker from here rather than from the whole
    catalogue, so a member isn't casually offered one brand's deal on a post
    about another. This is a convenience only: the picker runs in the browser,
    so the same rule is enforced again at upload (see routers/receipts.py),
    which is the check that actually counts.
    """
    mention = session.get(Mention, post_id)
    # Same 404 for "no such post" and "not yours" — whether a post id exists
    # isn't something one member should learn about another.
    if mention is None or mention.user_id != user.id:
        raise HTTPException(status_code=404, detail="Post not found.")

    reason, campaigns = brandtags.campaigns_for_post(mention, session)
    return PostCampaignsOut(
        reason=reason,
        taggedHandles=brandtags.decode_handles(mention) or [],
        campaigns=[_campaign_out(c) for c in campaigns],
    )


# Every call runs a paid Apify scrape, so keep it tight.
@router.post("/refresh", response_model=FeedRefreshOut,
             dependencies=[rate_limit("feed_refresh", limit=10, window=3600)])
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
        # Instagram's own displayUrl is a signed link that expires, so a post
        # shown later purely from storage (GET /feed, no re-scrape) can end up
        # pointing at a dead image — mirror it into our own storage now, while
        # it's still valid, so the copy we keep never goes stale.
        display_url = mirror_display_image(p.get("displayUrl"))
        tagged = extract_tagged_handles(p)
        fp = FeedPost(
            id=post_id,
            url=p.get("url"),
            displayUrl=display_url,
            caption=p.get("caption"),
            timestamp=p.get("timestamp"),
            ownerUsername=p.get("ownerUsername"),
            ownerFullName=p.get("ownerFullName"),
            likesCount=p.get("likesCount"),
            commentsCount=p.get("commentsCount"),
            taggedHandles=tagged,
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
                # Read now, while we still have the raw Apify item.
                tagged_handles=json.dumps(tagged),
                follower_count_at_scrape=profile_stats.get("followers") if profile_stats else None,
                following_count_at_scrape=profile_stats.get("following") if profile_stats else None,
                scraped_at=now,
            ))
    session.commit()

    return FeedRefreshOut(posts=posts, updated=now)
