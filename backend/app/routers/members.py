"""Admin member administration: the sign-up approval queue + account deletion.

New sign-ups land as `pending` (see `User.status`) and can't sign in until an
admin approves them here. The admin can also reject an account (it stays on
file, blocked) or delete it outright, which removes the person's posts and
cashback claims with it.

Admin-gated (reuses the campaigns X-Admin-Key).
"""
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlmodel import Session, select

from ..activity import log_activity
from ..db import get_session
from ..models import AdminMemberOut, Mention, Receipt, User
from ..storage import delete_receipt
from .campaigns import require_admin

router = APIRouter(prefix="/admin/members", tags=["admin"],
                   dependencies=[Depends(require_admin)])


def _who(user: User) -> str:
    """"Name — email", for the activity log."""
    name = f"{user.first_name} {user.last_name}".strip()
    return f"{name} — {user.email}" if name else user.email


def _set_status(user_id: int, new_status: str, session: Session) -> AdminMemberOut:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such member.")
    user.status = new_status
    session.add(user)
    session.commit()
    session.refresh(user)
    log_activity(session, f"{new_status.capitalize()} member", _who(user))
    return _member_out(user, session)


def _member_out(user: User, session: Session) -> AdminMemberOut:
    claims = len(session.exec(select(Receipt).where(Receipt.user_id == user.id)).all())
    posts = len(session.exec(select(Mention).where(Mention.user_id == user.id)).all())
    return AdminMemberOut(
        userId=user.id,
        firstName=user.first_name,
        lastName=user.last_name,
        email=user.email,
        instagramHandle=user.instagram_handle or "",
        status=user.status,
        createdAt=user.created_at,
        claims=claims,
        posts=posts,
    )


@router.get("", response_model=list[AdminMemberOut])
def list_members(status: str = Query(default="", description="pending / approved / rejected"),
                 session: Session = Depends(get_session)):
    """Member accounts, newest first. Optionally filtered to one status."""
    users = session.exec(select(User).order_by(User.id.desc())).all()
    if status:
        users = [u for u in users if u.status == status]

    # Count claims/posts once for everyone rather than per row.
    claims = Counter(r.user_id for r in session.exec(select(Receipt)).all())
    posts = Counter(m.user_id for m in session.exec(select(Mention)).all())
    return [
        AdminMemberOut(
            userId=u.id,
            firstName=u.first_name,
            lastName=u.last_name,
            email=u.email,
            instagramHandle=u.instagram_handle or "",
            status=u.status,
            createdAt=u.created_at,
            claims=claims.get(u.id, 0),
            posts=posts.get(u.id, 0),
        )
        for u in users
    ]


@router.post("/{user_id}/approve", response_model=AdminMemberOut)
def approve_member(user_id: int, session: Session = Depends(get_session)):
    """Let this account sign in."""
    return _set_status(user_id, "approved", session)


@router.post("/{user_id}/reject", response_model=AdminMemberOut)
def reject_member(user_id: int, session: Session = Depends(get_session)):
    """Block this account without deleting it (its sign-in stops working)."""
    return _set_status(user_id, "rejected", session)


@router.delete("/{user_id}", status_code=204)
def delete_member(user_id: int, session: Session = Depends(get_session)):
    """Delete an account for good, with the data that belongs to it.

    Removes the user's cashback claims (and their private receipt images) and
    their stored Instagram posts first, since both reference `user.id`.
    """
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such member.")

    receipts = session.exec(select(Receipt).where(Receipt.user_id == user_id)).all()
    mentions = session.exec(select(Mention).where(Mention.user_id == user_id)).all()
    for r in receipts:
        delete_receipt(r.image_key)
        session.delete(r)
    for m in mentions:
        session.delete(m)

    label = _who(user)
    session.delete(user)
    session.commit()
    log_activity(session, "Deleted member",
                 f"{label} ({len(receipts)} claim(s), {len(mentions)} post(s))")
    return Response(status_code=204)
