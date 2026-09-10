"""Single-use, expiring link tokens sent by email.

Used for verifying an email address, resetting a forgotten password, and
confirming a change of email address — for members and merchants alike.

Two rules make these safe:

  • Only the SHA-256 hash is stored. The raw token goes in the email and is
    never written down anywhere, so a database leak can't be replayed.
  • Issuing a new token of the same kind burns any earlier one. Click "forgot
    password" three times and only the newest link works — an old link sitting
    in an inbox (or a forwarded email) is already dead.
"""
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from .models import AuthToken

# How long each kind of link stays valid. Reset links are deliberately short:
# they're the most damaging to have stolen, and you use one immediately.
LIFETIME = {
    "verify_email": timedelta(hours=24),
    "reset_password": timedelta(hours=1),
    "change_email": timedelta(hours=1),
    # An invited merchant may not read their email for days, and there's no
    # self-serve way for them to ask for another one — only the admin can.
    "invite": timedelta(days=7),
}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue(session: Session, kind: str, subject_type: str, subject_id: int,
          new_email: str = "") -> str:
    """Mint a token and return the RAW value — the only time it ever exists.

    Put it in the emailed link. It cannot be recovered afterwards; if it's lost,
    issue a new one.
    """
    burn_outstanding(session, kind, subject_type, subject_id)

    # 32 bytes of randomness (~43 URL-safe characters). Far beyond guessable,
    # and safe to put straight into a URL.
    raw = secrets.token_urlsafe(32)
    session.add(AuthToken(
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
        token_hash=_hash(raw),
        new_email=new_email,
        expires_at=datetime.utcnow() + LIFETIME[kind],
    ))
    session.commit()
    return raw


def burn_outstanding(session: Session, kind: str, subject_type: str,
                     subject_id: int) -> None:
    """Mark every unused token of this kind for this subject as used."""
    rows = session.exec(
        select(AuthToken).where(
            AuthToken.kind == kind,
            AuthToken.subject_type == subject_type,
            AuthToken.subject_id == subject_id,
            AuthToken.used_at == None,                        # noqa: E711
        )
    ).all()
    now = datetime.utcnow()
    for row in rows:
        row.used_at = now
        session.add(row)
    if rows:
        session.commit()


def redeem(session: Session, raw: str, kind: str) -> Optional[AuthToken]:
    """Validate a token from a link and consume it. None if it isn't usable.

    Returns None for every failure mode — unknown, wrong kind, already used,
    expired — deliberately without saying which. The caller shows one generic
    "this link is invalid or has expired" either way, so a link can't be probed.

    Consumed on success, so a second click of the same link fails.
    """
    if not raw:
        return None
    token = session.exec(
        select(AuthToken).where(AuthToken.token_hash == _hash(raw))
    ).first()
    if token is None or token.kind != kind:
        return None
    if token.used_at is not None or token.expires_at < datetime.utcnow():
        return None
    token.used_at = datetime.utcnow()
    session.add(token)
    session.commit()
    return token
