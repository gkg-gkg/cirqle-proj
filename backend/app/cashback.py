"""Cashback clearing rules — time-based confirmation.

A claim's cashback is NOT confirmed the moment a receipt is reviewed. Instead it
"clears" automatically CONFIRM_DAYS after the Instagram post's date — matching the
countdown the feed already shows. So:

  • upload a receipt for a post  -> the claim is 'pending' (clearing)
  • CONFIRM_DAYS after the POST date -> it auto-becomes 'confirmed' (in the wallet)
  • admin can 'reject' a claim within the window; a withdrawn claim is 'paid'

We compute the effective status on read (no background job, no stored 'confirmed'
for new claims), so a claim flips to confirmed on its own once the 3 days pass.
Legacy rows already stored as 'confirmed' are kept as-is.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import Receipt

CONFIRM_DAYS = 3


def earn_to_amount(earn: str) -> float:
    """'£13.00' -> 13.0 ; '£0.90' -> 0.9 ; '' -> 0.0."""
    nums = re.findall(r"[\d.]+", earn or "")
    try:
        return float(nums[0]) if nums else 0.0
    except ValueError:
        return 0.0


def _naive_utc(dt: datetime) -> datetime:
    """Drop tzinfo (converting to UTC first) so we can compare against utcnow()."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def parse_post_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse an Instagram post timestamp (ISO 8601, possibly 'Z') to naive UTC."""
    if not ts:
        return None
    try:
        return _naive_utc(datetime.fromisoformat(ts.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def clears_at(receipt: Receipt, post_ts: Optional[datetime]) -> datetime:
    """When this claim's cashback clears: CONFIRM_DAYS after the post date, or the
    upload date if the post date is unknown."""
    return _naive_utc(post_ts or receipt.uploaded_at) + timedelta(days=CONFIRM_DAYS)


def effective_status(receipt: Receipt, post_ts: Optional[datetime]) -> str:
    """The member's view of a claim right now.

      pending   -> within the 3-day window (awaiting admin approval, or approved
                   and still clearing)
      confirmed -> approved within the window AND the 3 days have passed (in wallet)
      expired   -> the 3 days passed without approval (no cashback)
      rejected / paid -> as stored (legacy 'confirmed' is also returned as-is)
    """
    if receipt.status in ("paid", "rejected", "confirmed"):
        return receipt.status
    cleared = datetime.utcnow() >= clears_at(receipt, post_ts)
    if receipt.status == "verified":
        return "confirmed" if cleared else "pending"
    # 'pending' — not yet approved
    return "expired" if cleared else "pending"


def admin_status(receipt: Receipt, post_ts: Optional[datetime]) -> str:
    """The admin's view — like effective_status but keeps 'verified' (approved,
    still clearing) distinct so the review queue is clear about what needs action."""
    cleared = datetime.utcnow() >= clears_at(receipt, post_ts)
    if receipt.status == "verified":
        return "confirmed" if cleared else "verified"
    if receipt.status == "pending":
        return "expired" if cleared else "pending"
    return receipt.status  # confirmed (legacy) / rejected / paid
