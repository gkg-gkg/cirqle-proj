"""Auth endpoints: create an account, sign in, and fetch the current user."""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from ..db import get_session
from ..models import (AuthOut, PasswordChangeIn, ProfileUpdateIn, SigninIn,
                      SignupIn, SignupOut, User, UserOut)
from ..ratelimit import rate_limit
from ..security import (PENDING_MESSAGE, approval_error, create_token,
                        get_current_user, hash_password, verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])


def _normalize_handle(raw: str) -> str:
    """Trim, drop a leading @, lowercase — matches the frontend's normalisation."""
    return raw.strip().lstrip("@").lower()


def _user_out(user: User) -> UserOut:
    return UserOut(
        firstName=user.first_name,
        lastName=user.last_name,
        email=user.email,
        instagramHandle=user.instagram_handle,
        createdAt=user.created_at,
    )


@router.post("/signup", response_model=SignupOut, status_code=201,
             dependencies=[rate_limit("signup", limit=20, window=3600)])
def signup(data: SignupIn, session: Session = Depends(get_session)):
    email = data.email.lower()
    if session.exec(select(User).where(User.email == email)).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    if len(data.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters.")

    user = User(
        first_name=data.firstName.strip(),
        last_name=data.lastName.strip(),
        email=email,
        password_hash=hash_password(data.password),
        instagram_handle=_normalize_handle(data.instagramHandle),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    # No token: the account is `pending` until an admin approves it on
    # admin.html, so there is nothing to sign the browser into yet.
    return SignupOut(
        status=user.status,
        message=PENDING_MESSAGE,
        user=_user_out(user),
    )


@router.post("/signin", response_model=AuthOut,
             dependencies=[rate_limit("signin", limit=10, window=300)])
def signin(data: SigninIn, session: Session = Depends(get_session)):
    email = data.email.lower()
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    if user.status != "approved":
        raise approval_error(user.status)
    return AuthOut(token=create_token(user.id), user=_user_out(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return _user_out(user)


@router.patch("/me", response_model=UserOut)
def update_me(
    data: ProfileUpdateIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Let a logged-in user update their own profile: name, email, IG handle.

    PATCH semantics — only the fields present in the body are changed. Email
    must stay unique (it's the login identity), so we reject a clash with any
    other account.
    """
    if data.firstName is not None:
        first = data.firstName.strip()
        if not first:
            raise HTTPException(status_code=422, detail="First name can't be empty.")
        user.first_name = first

    if data.lastName is not None:
        last = data.lastName.strip()
        if not last:
            raise HTTPException(status_code=422, detail="Last name can't be empty.")
        user.last_name = last

    if data.email is not None:
        email = data.email.lower()
        clash = session.exec(select(User).where(User.email == email)).first()
        if clash is not None and clash.id != user.id:
            raise HTTPException(status_code=409, detail="An account with this email already exists.")
        user.email = email

    if data.instagramHandle is not None:
        handle = _normalize_handle(data.instagramHandle)
        if not handle:
            raise HTTPException(status_code=422, detail="Instagram handle can't be empty.")
        user.instagram_handle = handle

    session.add(user)
    session.commit()
    session.refresh(user)
    return _user_out(user)


@router.post("/me/password", status_code=204)
def change_password(
    data: PasswordChangeIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Change the signed-in user's password (verifies the current one first)."""
    if not verify_password(data.currentPassword, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    if len(data.newPassword) < 8:
        raise HTTPException(status_code=422, detail="New password must be at least 8 characters.")
    user.password_hash = hash_password(data.newPassword)
    session.add(user)
    session.commit()
    return Response(status_code=204)
