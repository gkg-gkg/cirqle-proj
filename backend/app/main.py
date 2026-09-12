"""Cirqle API entry point.

Run locally with:  uvicorn app.main:app --reload
Interactive docs:  http://localhost:8000/docs
"""
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()  # read backend/.env if present

from .routers import (account, adminlog, analytics, auth, campaigns, events,  # noqa: E402
                      feed, leaderboard, members, merchant, partners,
                      payouts_admin, receipts, stripe_webhook)
from .storage import MEDIA_DIR  # noqa: E402

app = FastAPI(title="Cirqle API")

# Which website origins may call this API. Set CIRQLE_CORS_ORIGINS (comma-
# separated) to override; otherwise default to our known production frontends.
# Any localhost port is always allowed via the regex below (for local dev), so
# we no longer fall back to a wide-open "*".
_env_origins = os.environ.get("CIRQLE_CORS_ORIGINS")
if _env_origins:
    origins = [o.strip() for o in _env_origins.split(",") if o.strip()]
else:
    origins = [
        "https://gkg-gkg.github.io",
        "https://cirqle.co.uk",
        "https://www.cirqle.co.uk",
    ]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(feed.router)
app.include_router(campaigns.router)
app.include_router(partners.router)
app.include_router(receipts.router)
app.include_router(account.router)
app.include_router(merchant.router)
app.include_router(events.router)
app.include_router(adminlog.router)
app.include_router(analytics.router)
app.include_router(members.router)
app.include_router(stripe_webhook.router)
app.include_router(payouts_admin.router)
app.include_router(leaderboard.router)

_log = logging.getLogger("cirqle.unhandled")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Without this, an unhandled exception is caught by Starlette's outermost
    ServerErrorMiddleware, which sits OUTSIDE CORSMiddleware — so the 500 it
    returns carries no Access-Control-Allow-Origin header, the browser treats
    it as a CORS failure, and the frontend reports a generic "can't reach the
    server" instead of the real error. Registering a handler here means
    ExceptionMiddleware (inside CORSMiddleware) handles it instead, so the
    response passes back through CORSMiddleware like any other response."""
    _log.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong on our end."})


# Serve locally-stored campaign images (only used when S3_BUCKET is unset; in
# prod images live on S3 and are served by AWS). mkdir so the mount never fails.
MEDIA_DIR.mkdir(exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")


# The schema is owned by Alembic (see backend/alembic). The deploy runs
# `alembic upgrade head` before starting the service, so we no longer create
# tables on startup — that couldn't add new columns to an existing database.


@app.get("/")
def health():
    return {"status": "ok", "service": "cirqle-api"}
