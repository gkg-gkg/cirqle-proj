"""A small in-memory rate limiter for abuse-prone endpoints (hardening).

Keyed by client IP + a bucket name, using a sliding window. It's per-process and
in-memory — fine for the single-box deployment; it resets on restart and isn't
shared across workers. For a larger setup, swap this for a Redis-backed limiter
(e.g. slowapi).

Client IP: behind our Caddy reverse proxy the real client is the LAST hop of
X-Forwarded-For (Caddy appends it); locally there's no proxy so we fall back to
the socket peer. Taking the last hop avoids a client spoofing an earlier value.

Three layers use it:
  - rate_limit(...)        per-endpoint limits on abuse-prone routes
  - GlobalRateLimit        a per-IP ceiling on every request, so nothing is unlimited
  - admin_guard(...)       counts FAILED admin-key attempts (see campaigns.require_admin)

Disable entirely with CIRQLE_RATE_LIMIT=off (used by the test suite).
"""
import os
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

# name -> ip -> deque[timestamps]
_hits: dict = defaultdict(lambda: defaultdict(deque))
# name -> its window in seconds, so the sweep knows when an IP's entry is stale
_windows: dict = {}

# Drop IPs with no recent hits every this-many checks. Without it every IP that
# ever called us stays in memory forever — fine for a handful of auth routes,
# not once the global limiter sees every request.
_SWEEP_EVERY = 5000
_calls = 0

# Global backstop: generous enough that no real page load or admin session gets
# near it (a full page is ~10 calls), tight enough to stop a script hammering us.
GLOBAL_LIMIT = int(os.environ.get("CIRQLE_GLOBAL_RATE_LIMIT", "300"))
GLOBAL_WINDOW = 60
# Stripe calls us server-to-server from a small set of IPs; never throttle it.
_GLOBAL_EXEMPT = ("/stripe/webhook",)


def _enabled() -> bool:
    return os.environ.get("CIRQLE_RATE_LIMIT", "on").strip().lower() not in ("off", "0", "false", "no")


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _sweep(now: float) -> None:
    for name, by_ip in _hits.items():
        window = _windows.get(name, 3600)
        stale = [ip for ip, q in by_ip.items() if not q or q[-1] < now - window]
        for ip in stale:
            del by_ip[ip]


def _retry_after(name: str, ip: str, limit: int, window: int, record: bool = True) -> int:
    """Check (and by default record) one hit. Returns 0 if allowed, otherwise
    the seconds until the oldest hit in the window expires."""
    global _calls
    now = time.monotonic()
    _windows[name] = window
    _calls += 1
    if _calls % _SWEEP_EVERY == 0:
        _sweep(now)
    q = _hits[name][ip]
    cutoff = now - window
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= limit:
        return max(1, int(q[0] + window - now) + 1)
    if record:
        q.append(now)
    return 0


def _too_many(retry: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many attempts. Please wait a moment and try again.",
        headers={"Retry-After": str(retry)},
    )


def rate_limit(name: str, limit: int, window: int):
    """A FastAPI dependency allowing `limit` requests per `window` seconds per
    client IP for this `name` bucket. Raises 429 when exceeded."""
    def dep(request: Request) -> None:
        if not _enabled():
            return
        retry = _retry_after(name, _client_ip(request), limit, window)
        if retry:
            raise _too_many(retry)
    return Depends(dep)


# Failed admin-key attempts: 10 wrong guesses per IP per 15 minutes, then that
# IP is locked out (even with the right key) until the window clears.
_ADMIN_FAIL_LIMIT = 10
_ADMIN_FAIL_WINDOW = 900


def admin_guard(request: Request, key_ok: bool) -> None:
    """Called by require_admin. Only failures count, so a busy admin session
    never trips it; a lockout blocks the correct key too, or guessing would
    just carry on and the right answer would still get through."""
    if not _enabled():
        return
    ip = _client_ip(request)
    retry = _retry_after("admin_fail", ip, _ADMIN_FAIL_LIMIT, _ADMIN_FAIL_WINDOW, record=False)
    if retry:
        raise _too_many(retry)
    if not key_ok:
        _retry_after("admin_fail", ip, _ADMIN_FAIL_LIMIT, _ADMIN_FAIL_WINDOW)


class GlobalRateLimit:
    """ASGI middleware: at most GLOBAL_LIMIT requests per GLOBAL_WINDOW seconds
    per IP, across the whole API. Must be added BEFORE CORSMiddleware in
    main.py so CORS wraps it — otherwise the 429 has no CORS headers and the
    browser reports it as "can't reach the server"."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or not _enabled()
                or scope["path"].startswith(_GLOBAL_EXEMPT)):
            return await self.app(scope, receive, send)
        retry = _retry_after("global", _client_ip(Request(scope)), GLOBAL_LIMIT, GLOBAL_WINDOW)
        if retry:
            resp = JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please wait a moment and try again."},
                headers={"Retry-After": str(retry)},
            )
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)
