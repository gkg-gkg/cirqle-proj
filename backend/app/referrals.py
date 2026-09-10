"""Referral bonuses — when they're earned, who funds them, and when they aren't.

A genuine referral pays both sides:

    A posts about a deal and their own claim is approved.
    B buys through A's post and uploads a receipt naming @A.
    B's claim is approved and clears its 3-day hold.
      -> A gets £1, B gets 50p, both from the merchant's referral wallet.

Both hang off B's claim, and both are created at the same moment, because B's
claim clearing is the only evidence the referral was real. A's own 3-day hold is
about A's own cashback and has nothing to do with this.

Nothing here runs on a timer. Cashback in this codebase clears by the clock
rather than through a background job (see cashback.py), so a referral becomes
payable at a moment no code is awake for. Settling therefore happens on read:
whenever a member's wallet or dashboard is worked out, anything they've earned
since last time is settled first. That makes settling safe to call often, and
safe to call twice — the UNIQUE on (receipt_id, kind) means a repeat call can't
pay for the same claim again.

The checks are strict on purpose, and they fail CLOSED: anything we can't
confirm blocks the money rather than waving it through. What they exist to stop
is one person referring themselves from a second account, and the last line of
defence is that Stripe has identity-checked both people before either is paid.

Every reason a bonus is withheld is a sentence written for the member, because a
bonus that silently never arrives is worse than one that explains itself.
"""
from sqlmodel import Session, select

from .cashback import (APPROVED_STATUSES, _naive_utc, effective_status,
                       parse_post_ts)
from .models import (Campaign, Mention, MerchantTransaction, Receipt,
                     ReferralReward, User)

# What one verified referral pays, funded by the merchant rather than by us.
REWARD_REFERRER = 1.0      # to the member whose post drove the purchase
REWARD_REFEREE = 0.5       # to the member who bought through it
REFERRAL_COST = REWARD_REFERRER + REWARD_REFEREE

# Rewards that have cost the merchant money. A cancelled one has not.
_SPENT_STATUSES = ("available", "paid")


# ── The merchant's referral wallet ───────────────────────────────────────────

def referral_topups(merchant_id: int, session: Session) -> float:
    """£ this merchant has put into their referral wallet."""
    rows = session.exec(
        select(MerchantTransaction).where(
            MerchantTransaction.merchant_id == merchant_id,
            MerchantTransaction.wallet == "referral",
            MerchantTransaction.kind.in_(("topup", "refund")),
        )
    ).all()
    return round(sum(t.amount for t in rows), 2)


def referral_spend(merchant_id: int, session: Session) -> float:
    """£ of referral bonuses this merchant has actually funded."""
    rows = session.exec(
        select(ReferralReward).where(
            ReferralReward.merchant_id == merchant_id,
            ReferralReward.status.in_(_SPENT_STATUSES),
        )
    ).all()
    return round(sum(r.amount for r in rows), 2)


def referral_balance(merchant_id: int, session: Session) -> float:
    """What's left in the referral wallet. Separate from the cashback wallet:
    a full cashback balance does not pay for a single referral bonus."""
    return round(referral_topups(merchant_id, session)
                 - referral_spend(merchant_id, session), 2)


# ── Has this referral earned its bonuses? ────────────────────────────────────

def _moment(receipt: Receipt, session: Session):
    """When this claim's purchase effectively happened — the Instagram post's
    date if we have it, else when the receipt was uploaded.

    Always naive UTC. Upload timestamps are written both ways in this codebase
    (utcnow() on create, an aware value on replace), and comparing the two kinds
    raises rather than returning a wrong answer.
    """
    mention = session.get(Mention, receipt.post_id)
    posted = parse_post_ts(mention.timestamp) if mention else None
    return posted or _naive_utc(receipt.uploaded_at)


def _cleared(receipt: Receipt, session: Session) -> bool:
    mention = session.get(Mention, receipt.post_id)
    status = effective_status(receipt, parse_post_ts(mention.timestamp) if mention else None)
    return status in ("confirmed", "paid")


def _same_person(a: User, b: User) -> tuple[bool, str]:
    """Do these two accounts look like one person? -> (yes/no, why not payable).

    Compares what Stripe verified against documents — the bank account it will
    pay into, and the name and date of birth on the account. We hold only hashes
    of both (see payments._fingerprint), which is all a comparison needs.

    Fails CLOSED. With nothing to compare we cannot rule out that these are the
    same person, so no bonus is paid. A verified Express account carries both, so
    an empty fingerprint means something is wrong on our side — not that the two
    are different people.
    """
    if not (a.payout_fingerprint and b.payout_fingerprint):
        return True, "We're still waiting on bank details from Stripe."
    if not (a.identity_fingerprint and b.identity_fingerprint):
        return True, "We're still waiting on identity details from Stripe."
    if a.payout_fingerprint == b.payout_fingerprint:
        return True, "These accounts share bank details."
    if a.identity_fingerprint == b.identity_fingerprint:
        return True, "These accounts share identity details."
    return False, ""


def check(receipt: Receipt, session: Session) -> tuple[bool, str]:
    """Has this referred claim earned its bonuses? -> (yes/no, why not).

    Re-run from scratch every time rather than trusting what was recorded at
    upload: a referrer whose own claim was merely 'pending' back then may since
    have been approved, or rejected.
    """
    if receipt.referred_by_user_id is None:
        return False, "No referral on this claim."
    if receipt.campaign_id is None:
        return False, "This claim isn't linked to a deal."

    referrer = session.get(User, receipt.referred_by_user_id)
    if referrer is None:
        return False, "The referrer's account no longer exists."

    campaign = session.get(Campaign, receipt.campaign_id)
    if campaign is None:
        return False, "That deal no longer exists."
    if not campaign.referrals_enabled:
        return False, "This deal doesn't offer a referral bonus."

    if not _cleared(receipt, session):
        return False, "The claim hasn't cleared yet."

    # The referrer must have promoted this same deal themselves, and their own
    # claim must have been approved — one still in the queue earns nothing yet.
    own_claims = session.exec(
        select(Receipt).where(
            Receipt.user_id == referrer.id,
            Receipt.campaign_id == receipt.campaign_id,
        ).order_by(Receipt.uploaded_at)
    ).all()
    approved = [c for c in own_claims if c.status in APPROVED_STATUSES]
    if not approved:
        return False, "The referrer's own claim for this deal hasn't been approved."

    # You can't have referred someone to a deal you posted about afterwards.
    if _moment(approved[0], session) > _moment(receipt, session):
        return False, "The referrer's post came after the purchase."

    # Only the first claim a member makes on a deal can be a referral. Without
    # this, a regular who already buys from a brand could be "referred" to it
    # again and again.
    first = session.exec(
        select(Receipt).where(
            Receipt.user_id == receipt.user_id,
            Receipt.campaign_id == receipt.campaign_id,
            Receipt.status != "rejected",
        ).order_by(Receipt.uploaded_at)
    ).first()
    if first is not None and first.id != receipt.id:
        return False, "They had already claimed this deal before this one."

    referee = session.get(User, receipt.user_id)
    if referee is None:
        return False, "That member's account no longer exists."

    # Both sides must have been through Stripe's identity checks, because that
    # is what the same-person comparison reads. Held, not refused: they can
    # still finish verifying, and the bonuses are paid when they do.
    if not (referrer.payouts_enabled and referee.payouts_enabled):
        return False, "Waiting for both members to finish identity verification."

    same, why = _same_person(referrer, referee)
    if same:
        return False, why

    if campaign.merchant_id is None:
        return False, "This deal has no brand to fund the bonus."

    # Both bonuses come from the same wallet and are created together, so the
    # wallet has to cover the pair. A wallet holding £1 funds no referral at all
    # rather than half of one.
    if referral_balance(campaign.merchant_id, session) < REFERRAL_COST:
        return False, "The brand's referral wallet is empty."

    return True, ""


# ── Creating, cancelling and totting up bonuses ──────────────────────────────

def settle_receipt(receipt: Receipt, session: Session) -> bool:
    """Create both bonuses for one referred claim, if it has earned them.

    All or nothing: either the referral is genuine and both sides are paid, or
    neither is. Returns whether anything was created.
    """
    if session.exec(select(ReferralReward).where(
            ReferralReward.receipt_id == receipt.id)).first() is not None:
        return False
    ok, _reason = check(receipt, session)
    if not ok:
        return False

    campaign = session.get(Campaign, receipt.campaign_id)
    for user_id, kind, amount in (
        (receipt.referred_by_user_id, "referrer", REWARD_REFERRER),
        (receipt.user_id, "referee", REWARD_REFEREE),
    ):
        session.add(ReferralReward(
            user_id=user_id, kind=kind, receipt_id=receipt.id,
            campaign_id=receipt.campaign_id, merchant_id=campaign.merchant_id,
            amount=amount, status="available",
        ))
    # Flush so the next claim's wallet check sees this money already committed —
    # otherwise a wallet with £1.50 left could fund two referrals at once.
    session.flush()
    return True


def _involving(user: User, session: Session) -> list:
    """Every referred claim this member is part of, on either side."""
    referred = session.exec(
        select(Receipt).where(Receipt.referred_by_user_id == user.id)
    ).all()
    own = session.exec(
        select(Receipt).where(
            Receipt.user_id == user.id,
            Receipt.referred_by_user_id.is_not(None),
        )
    ).all()
    return list({r.id: r for r in list(referred) + list(own)}.values())


def settle_for_user(user: User, session: Session) -> None:
    """Create any bonuses this member has earned but not yet been given —
    both the £1s for people they referred and the 50p for being referred."""
    created = False
    for receipt in _involving(user, session):
        created = settle_receipt(receipt, session) or created
    if created:
        session.commit()


def cancel_for_receipt(receipt: Receipt, session: Session, reason: str) -> None:
    """Take back the bonuses a claim earned, when that claim is later rejected.

    A bonus already withdrawn is left alone: the money has reached someone's
    bank and cancelling the row here would only make our books disagree with
    reality. Recovering that is a conversation, not a database update.
    """
    for reward in session.exec(select(ReferralReward).where(
            ReferralReward.receipt_id == receipt.id)).all():
        if reward.status != "available":
            continue
        reward.status = "cancelled"
        reward.cancel_reason = reason
        session.add(reward)


def totals(user_id: int, session: Session) -> tuple[float, float, int]:
    """(£ available to withdraw, £ already withdrawn, bonuses credited)."""
    rows = session.exec(
        select(ReferralReward).where(ReferralReward.user_id == user_id)
    ).all()
    available = round(sum(r.amount for r in rows if r.status == "available"), 2)
    paid = round(sum(r.amount for r in rows if r.status == "paid"), 2)
    return available, paid, len([r for r in rows if r.status in _SPENT_STATUSES])


def available_rewards(user_id: int, session: Session) -> list:
    """The reward rows a withdrawal would cash in."""
    return session.exec(
        select(ReferralReward).where(
            ReferralReward.user_id == user_id,
            ReferralReward.status == "available",
        )
    ).all()


def summary(user: User, session: Session) -> list[dict]:
    """Every referral this member is part of, and for unpaid ones, why.

    Drives the member's dashboard: a bonus that hasn't arrived should say what
    it's waiting for.
    """
    claims = sorted(_involving(user, session),
                    key=lambda r: r.uploaded_at, reverse=True)
    rewards = {(r.receipt_id, r.kind): r for r in session.exec(
        select(ReferralReward).where(ReferralReward.user_id == user.id)).all()}

    out = []
    for receipt in claims:
        role = "referrer" if receipt.user_id != user.id else "referee"
        reward = rewards.get((receipt.id, role))
        if reward is not None and reward.status in _SPENT_STATUSES:
            status, reason, amount = "earned", "", reward.amount
        elif reward is not None:
            status, reason, amount = "cancelled", reward.cancel_reason, 0.0
        else:
            _ok, reason = check(receipt, session)
            status, amount = "waiting", 0.0
        # Name the other person: "who was this with" is the first thing a member
        # looks for. The referrer sees the buyer's handle, and the buyer theirs.
        other_id = receipt.user_id if role == "referrer" else receipt.referred_by_user_id
        other = session.get(User, other_id)
        out.append({
            "receiptId": receipt.id,
            "brand": receipt.brand,
            "role": role,
            "handle": (other.instagram_handle if other else "") or "",
            "amount": amount,
            "status": status,
            "reason": reason,
            "date": receipt.uploaded_at,
        })
    return out
