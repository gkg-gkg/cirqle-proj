"""Auth endpoints: create an account, sign in, and fetch the current user."""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import mailer, passwords, tokens
from ..db import get_session
from ..handles import normalize_handle
from ..models import (AuthOut, EmailIn, MessageOut, PasswordChangeIn,
                      ProfileUpdateIn, ResetPasswordIn, SigninIn, SignupIn,
                      SignupOut, TokenIn, TokenOut, User, UserOut)
from ..ratelimit import rate_limit
from ..security import (approval_error, create_token, get_current_user,
                        hash_password, verify_password)

# Returned by every "we've emailed you" endpoint, whether or not the address is
# real. Saying "no such account" would let anyone test which emails are members.
SENT_MESSAGE = ("If that email address has a Cirqle account, we've sent a link "
                "to it. Check your inbox.")
VERIFY_MESSAGE = ("Check your inbox — we've sent a link to confirm your email "
                  "address. Once confirmed, we'll review your account.")

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_out(user: User) -> UserOut:
    return UserOut(
        firstName=user.first_name,
        lastName=user.last_name,
        email=user.email,
        instagramHandle=user.instagram_handle,
        createdAt=user.created_at,
        pendingEmail=user.pending_email or "",
    )


@router.post("/signup", response_model=SignupOut, status_code=201,
             dependencies=[rate_limit("signup", limit=20, window=3600)])
def signup(data: SignupIn, session: Session = Depends(get_session)):
    email = data.email.lower()
    if session.exec(select(User).where(User.email == email)).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    passwords.validate(data.password, email)

    user = User(
        first_name=data.firstName.strip(),
        last_name=data.lastName.strip(),
        email=email,
        password_hash=hash_password(data.password),
        instagram_handle=normalize_handle(data.instagramHandle),
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    # Two gates now stand in front of this account: confirm the address, then an
    # admin approves. It stays out of the admin's queue until the first is done,
    # so nobody reviews a mistyped or fake address.
    raw = tokens.issue(session, "verify_email", "user", user.id)
    mailer.send_verification(user.email, user.first_name, raw)

    # No token: there is nothing to sign the browser into yet.
    return SignupOut(
        status=user.status,
        message=VERIFY_MESSAGE,
        user=_user_out(user),
    )


@router.post("/signin", response_model=AuthOut,
             dependencies=[rate_limit("signin", limit=10, window=300)])
def signin(data: SigninIn, session: Session = Depends(get_session)):
    email = data.email.lower()
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        # Hash anyway so an unknown email takes as long as a known one. Without
        # this, response timing alone reveals which addresses have accounts.
        hash_password(data.password)
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    if not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    if user.status != "approved":
        raise approval_error(user.status)
    return AuthOut(token=create_token(user), user=_user_out(user))


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

    PATCH semantics — only the fields present in the body are changed. Names and
    handle apply immediately; a NEW email address does not. It's recorded on
    `pending_email` and a confirmation link goes to that address, so the account
    only moves once someone proves they can read the new inbox.
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
        if email != user.email:
            clash = session.exec(select(User).where(User.email == email)).first()
            if clash is not None and clash.id != user.id:
                raise HTTPException(status_code=409, detail="An account with this email already exists.")
            # Deliberately NOT applied here. The new address has to be proven
            # first, otherwise a stolen login token could quietly move the
            # account to an attacker's address and lock the owner out. It is
            # recorded as pending and applied by /auth/confirm-email-change.
            user.pending_email = email
            raw = tokens.issue(session, "change_email", "user", user.id,
                               new_email=email)
            mailer.send_email_change(email, user.first_name, raw)

    if data.instagramHandle is not None:
        handle = normalize_handle(data.instagramHandle)
        if not handle:
            raise HTTPException(status_code=422, detail="Instagram handle can't be empty.")
        user.instagram_handle = handle

    session.add(user)
    session.commit()
    session.refresh(user)
    return _user_out(user)


@router.post("/me/password", response_model=TokenOut)
def change_password(
    data: PasswordChangeIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Change the signed-in user's password (verifies the current one first)."""
    if not verify_password(data.currentPassword, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    passwords.validate(data.newPassword, user.email)
    user.password_hash = hash_password(data.newPassword)
    # Moves the "pwd" claim, which invalidates every token issued before now —
    # including this caller's. They get a replacement below so the page they're
    # on keeps working; every other device is signed out.
    user.password_changed_at = datetime.utcnow()
    session.add(user)
    session.commit()
    session.refresh(user)
    return TokenOut(token=create_token(user))


# ── Email verification ───────────────────────────────────────────────────────

@router.post("/verify-email", response_model=MessageOut,
             dependencies=[rate_limit("verify", limit=20, window=3600)])
def verify_email(data: TokenIn, session: Session = Depends(get_session)):
    """Redeem the link from the signup email. This is the first of the two
    gates: it moves the account into the admin's approval queue."""
    token = tokens.redeem(session, data.token, "verify_email")
    if token is None or token.subject_type != "user":
        raise HTTPException(
            status_code=400,
            detail="This link is invalid or has expired. Request a new one.")
    user = session.get(User, token.subject_id)
    if user is None:
        raise HTTPException(status_code=400, detail="This link is no longer valid.")

    if user.email_verified_at is None:
        user.email_verified_at = datetime.utcnow()
        # Only advance an account still waiting on its email. An account the
        # admin already approved or rejected must not be dragged back to
        # 'pending' by someone clicking an old link.
        if user.status == "unverified":
            user.status = "pending"
        session.add(user)
        session.commit()

    return MessageOut(message=(
        "Email confirmed. Your account is now with our team for approval — "
        "we'll email you the moment it's live."))


@router.post("/resend-verification", response_model=MessageOut,
             dependencies=[rate_limit("resend", limit=5, window=3600)])
def resend_verification(data: EmailIn, session: Session = Depends(get_session)):
    """Send another verification link. Answers identically for an address with
    no account, one already verified, and one we actually emailed."""
    user = session.exec(select(User).where(User.email == data.email.lower())).first()
    if user is not None and user.email_verified_at is None:
        raw = tokens.issue(session, "verify_email", "user", user.id)
        mailer.send_verification(user.email, user.first_name, raw)
    return MessageOut(message=SENT_MESSAGE)


# ── Forgotten password ───────────────────────────────────────────────────────

@router.post("/forgot-password", response_model=MessageOut,
             dependencies=[rate_limit("forgot", limit=5, window=3600)])
def forgot_password(data: EmailIn, session: Session = Depends(get_session)):
    """Email a reset link. Always reports success — see SENT_MESSAGE.

    Rejected accounts are skipped silently: there's no point resetting into an
    account that can't sign in, and saying so would leak the account's state.
    """
    user = session.exec(select(User).where(User.email == data.email.lower())).first()
    if user is not None and user.status != "rejected":
        raw = tokens.issue(session, "reset_password", "user", user.id)
        mailer.send_password_reset(user.email, user.first_name, raw)
    return MessageOut(message=SENT_MESSAGE)


@router.post("/reset-password", response_model=MessageOut,
             dependencies=[rate_limit("reset", limit=10, window=3600)])
def reset_password(data: ResetPasswordIn, session: Session = Depends(get_session)):
    """Set a new password from a reset link, then sign every device out."""
    token = tokens.redeem(session, data.token, "reset_password")
    if token is None or token.subject_type != "user":
        raise HTTPException(
            status_code=400,
            detail="This link is invalid or has expired. Request a new one.")
    user = session.get(User, token.subject_id)
    if user is None:
        raise HTTPException(status_code=400, detail="This link is no longer valid.")
    passwords.validate(data.newPassword, user.email)

    user.password_hash = hash_password(data.newPassword)
    # Moving this invalidates every existing login token — the whole point of a
    # reset when someone else may have had access.
    user.password_changed_at = datetime.utcnow()
    # Reaching the inbox proves the address works, so a reset also verifies it.
    if user.email_verified_at is None:
        user.email_verified_at = datetime.utcnow()
        if user.status == "unverified":
            user.status = "pending"
    session.add(user)
    session.commit()

    # No login token is returned: they sign in with the new password, which
    # proves it's the value they intended.
    return MessageOut(message="Password updated. You can now sign in.")


# ── Confirming a change of email address ─────────────────────────────────────

@router.post("/confirm-email-change", response_model=MessageOut,
             dependencies=[rate_limit("verify", limit=20, window=3600)])
def confirm_email_change(data: TokenIn, session: Session = Depends(get_session)):
    """Apply a pending email change, once the new address has been proven."""
    token = tokens.redeem(session, data.token, "change_email")
    if token is None or token.subject_type != "user":
        raise HTTPException(
            status_code=400,
            detail="This link is invalid or has expired. Request a new one.")
    user = session.get(User, token.subject_id)
    if user is None or not token.new_email:
        raise HTTPException(status_code=400, detail="This link is no longer valid.")

    # Someone else may have taken the address in the time between requesting
    # the change and confirming it.
    clash = session.exec(select(User).where(User.email == token.new_email)).first()
    if clash is not None and clash.id != user.id:
        raise HTTPException(
            status_code=409,
            detail="That email address is now in use by another account.")

    user.email = token.new_email
    user.pending_email = ""
    user.email_verified_at = datetime.utcnow()
    session.add(user)
    session.commit()
    return MessageOut(message="Email address updated.")
