"""Server-side Instagram scraping via Apify.

The Apify token lives here (read from the APIFY_TOKEN environment variable) and
NEVER reaches the browser — that is the whole point of Phase 2. The frontend
asks our API to refresh; our API does the scrape and returns only the finished
posts.

We scrape the *brand* account's mentions (posts that tag @cirqle.co.uk) and let
the caller filter down to a single user's own posts.
"""
import os
import time
from typing import Optional

import httpx
from apify_client import ApifyClient

from .storage import upload_image_bytes

# apify/instagram-scraper — same actor the old client-side code used.
ACTOR_ID = "apify/instagram-scraper"

# Instagram account whose mentions we scrape. Override with CIRQLE_BRAND_HANDLE.
# cirqle.ltd was never the live account's handle — the real one is cirqle.co.uk,
# so scraping the old default found nothing: users were told in the site's own
# copy to tag @cirqle, which Instagram either turned into a dead link or, worse,
# tagged a stranger's account, and even a correct tag of @cirqle.co.uk still
# wouldn't have been picked up by this scraper looking at cirqle.ltd.
BRAND_HANDLE = os.environ.get("CIRQLE_BRAND_HANDLE", "cirqle.co.uk")

# Every user's feed is filtered from the SAME brand-wide scrape, so a burst of
# refreshes would otherwise fire many identical ~1-min Apify runs. Cache the raw
# scrape for a short window and reuse it. (In-memory: fine for our single server
# process; move to Redis if we ever run multiple workers.)
_CACHE_TTL_SECONDS = 60
_cache: dict = {"at": 0.0, "limit": None, "posts": None}

# A user's own follower/following counts change slowly, so this cache lives
# much longer than the brand-mentions one above. Keyed by normalized handle.
_PROFILE_CACHE_TTL_SECONDS = 900
_profile_cache: dict = {}


class ScrapeError(RuntimeError):
    """Raised when we cannot complete a scrape (missing token or Apify failure)."""


def _do_scrape(token: str, limit: int) -> list[dict]:
    """The actual Apify run — no caching. Raises ScrapeError on failure."""
    client = ApifyClient(token)
    run_input = {
        "directUrls": [f"https://www.instagram.com/{BRAND_HANDLE}/"],
        "resultsType": "mentions",   # posts where the brand is tagged
        "resultsLimit": limit,
    }
    try:
        # .call() starts the run and blocks until it finishes (~1 min).
        run = client.actor(ACTOR_ID).call(run_input=run_input)
        dataset = client.dataset(run["defaultDatasetId"])
        return list(dataset.iterate_items())
    except Exception as exc:  # noqa: BLE001 — surface any Apify failure as one type
        raise ScrapeError(f"Instagram scrape failed: {exc}") from exc


def scrape_brand_mentions(limit: int = 50) -> list[dict]:
    """Return posts that tag the brand account, reusing a recent scrape if fresh.

    Raises ScrapeError if the token is missing or the Apify run fails.
    """
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise ScrapeError("Server is missing APIFY_TOKEN — set it in the backend .env.")

    fresh = (
        _cache["posts"] is not None
        and _cache["limit"] == limit
        and (time.time() - _cache["at"]) < _CACHE_TTL_SECONDS
    )
    if fresh:
        return _cache["posts"]

    posts = _do_scrape(token, limit)
    _cache.update(at=time.time(), limit=limit, posts=posts)
    return posts


def _do_scrape_profile(token: str, handle: str) -> Optional[dict]:
    """One Apify run against a single profile URL, 'details' mode. No caching."""
    client = ApifyClient(token)
    run_input = {
        "directUrls": [f"https://www.instagram.com/{handle}/"],
        "resultsType": "details",   # profile-level stats, not posts
        "resultsLimit": 1,
    }
    run = client.actor(ACTOR_ID).call(run_input=run_input)
    dataset = client.dataset(run["defaultDatasetId"])
    items = list(dataset.iterate_items())
    if not items:
        return None
    item = items[0]
    followers = item.get("followersCount")
    following = item.get("followsCount")
    if followers is None and following is None:
        return None
    return {"followers": followers, "following": following}


def scrape_profile_stats(handle: str) -> Optional[dict]:
    """Best-effort: this user's own followers/following counts.

    Never raises — a broken or rate-limited profile lookup must not block the
    mention refresh it's called alongside. Returns None on any failure
    (missing token, Apify error, unexpected response shape) or unrecognised
    handle, and a fresh {"followers": int|None, "following": int|None} dict
    on success, reusing a longer-lived cache since these numbers move slowly.
    """
    if not handle:
        return None

    cached = _profile_cache.get(handle)
    if cached and (time.time() - cached["at"]) < _PROFILE_CACHE_TTL_SECONDS:
        return cached["stats"]

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        return None

    try:
        stats = _do_scrape_profile(token, handle)
    except Exception:  # noqa: BLE001 — this path must never raise
        stats = None

    _profile_cache[handle] = {"at": time.time(), "stats": stats}
    return stats


def mirror_display_image(url: Optional[str]) -> Optional[str]:
    """Download a post's Instagram CDN image and re-store it as our own.

    displayUrl comes straight from Apify as Instagram's own signed CDN link,
    which expires (observed: stale within a day or two) — so a post scraped
    once and then only ever read back via GET /feed (no re-scrape) eventually
    shows "No image" even though the post itself is still there. Mirroring it
    into our own storage at scrape time, the same way a campaign photo upload
    is stored, makes the URL permanent.

    Best-effort and never raises: on any failure (network, non-image response,
    storage down) the original Instagram URL is returned unchanged, so a scrape
    still succeeds — that post just carries the old expiring link rather than
    the refresh failing outright.
    """
    if not url:
        return url
    try:
        resp = httpx.get(url, timeout=8.0, follow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            return url
        return upload_image_bytes(resp.content, content_type)
    except Exception:  # noqa: BLE001 — mirroring is best-effort
        return url
