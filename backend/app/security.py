"""Password hashing and login tokens.

- Passwords are hashed with bcrypt (a slow, salted hash) — we never store the
  raw password, so a database leak doesn't expose anyone's password.
- Login is proven with a JWT: a signed token the browser sends on each request.
  We can verify it with our secret key without a database lookup for the token
  itself, then load the user it points to.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlmodel import Session

from .db import get_session
from .models import Merchant, User

SECRET_KEY = os.environ.get("CIRQLE_SECRET_KEY", "dev-insecure-change-me")
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 7

# Reads the "Authorization: Bearer <token>" header off incoming requests.
bearer_scheme = HTTPBearer(auto_error=True)

# Approval gate: a member account is only usable once an admin approves it.
# Shown to an account that can't sign in yet.
PENDING_MESSAGE = ("Your account is awaiting approval. We'll let you know once "
                   "it's live.")
REJECTED_MESSAGE = "This account hasn't been approved for Cirqle."


def approval_error(status: str) -> HTTPException:
    """403 explaining why a not-yet-approved account is being turned away."""
    return HTTPException(
        status_code=403,
        detail=PENDING_MESSAGE if status == "pending" else REJECTED_MESSAGE,
    )


def hash_password(password: str) -> str:
    # bcrypt only uses the first 72 bytes; trim to avoid an error on long input.
    pw = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    pw = password.encode("utf-8")[:72]
    return bcrypt.checkpw(pw, password_hash.encode("utf-8"))


def create_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> User:
    """Decode the bearer token and return the matching user, or raise 401.

    Merchant tokens (typ="merchant") are rejected here so a merchant login can't
    reach user endpoints.
    """
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token.",
    )
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("typ") == "merchant":
            raise invalid
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise invalid
    user = session.get(User, user_id)
    if user is None:
        raise invalid
    # A token minted before approval (or before the account was rejected /
    # revoked) stops working here, not just at sign-in.
    if user.status != "approved":
        raise approval_error(user.status)
    return user


def create_merchant_token(merchant_id: int) -> str:
    payload = {
        "sub": str(merchant_id),
        "typ": "merchant",
        "exp": datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_merchant(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> Merchant:
    """Decode a merchant bearer token and return the merchant, or raise 401.

    Only accepts tokens minted with typ="merchant" (a user token won't work).
    """
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token.",
    )
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("typ") != "merchant":
            raise invalid
        merchant_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise invalid
    merchant = session.get(Merchant, merchant_id)
    if merchant is None:
        raise invalid
    return merchant
