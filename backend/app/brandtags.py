"""Which brand a member actually tagged, and whether their claim agrees.

A post reaches us because it tags @cirqle.co.uk (see app/instagram.py). That is
what makes it a Cirqle post — it says nothing about which BRAND it is about.
The member says that separately, by picking a campaign when they upload the
receipt, and until this module existed that pick was taken entirely on trust:
tag one brand, claim another brand's deal, and the wrong merchant is billed for
a post that never mentioned them.

So we read the other accounts tagged in the post, map them to merchants by
their stored Instagram handle, and compare that against the campaign chosen.

One distinction runs through all of it: a post whose tags we never captured is
UNKNOWN, and unknown never counts against anyone. Only a post we positively
read, which positively names a different Cirqle brand, is a contradiction.
"""
import json
from typing import Optional

from sqlmodel import Session, select

from .handles import normalize_handle
from .models import Campaign, Merchant, Mention

# How a claim's chosen campaign compares with the tags on its post. Stamped on
# Receipt.tag_match, and returned to the upload page so it can explain itself.
MATCHED = "matched"                   # the campaign's brand is tagged in the post
MISMATCH = "mismatch"                 # a DIFFERENT Cirqle brand is — rejected at upload
NO_BRAND_TAGGED = "no_brand_tagged"   # we read the tags; no Cirqle brand among them
NO_TAG_DATA = "no_tag_data"           # we never captured this post's tags
UNVERIFIABLE = "unverifiable"         # no campaign, or its brand has no handle on file


def decode_handles(mention: Optional[Mention]) -> Optional[list[str]]:
    """The handles tagged in a post — or None when we never looked.

    None and [] are both falsy and mean opposite things here, so callers must
    test `is None` rather than truthiness.
    """
    if mention is None or mention.tagged_handles is None:
        return None
    try:
        raw = json.loads(mention.tagged_handles)
    except ValueError:
        # Corrupt JSON is no evidence, not evidence of absence.
        return None
    return [h for h in raw if isinstance(h, str)]


def campaign_handle(campaign: Optional[Campaign], session: Session) -> str:
    """The Instagram handle of the brand behind a campaign, '' if unknown.

    Empty for an admin-authored campaign with no merchant attached, and for a
    merchant who never filled their handle in — in both cases there is nothing
    to compare a tag against.
    """
    if campaign is None or campaign.merchant_id is None:
        return ""
    merchant = session.get(Merchant, campaign.merchant_id)
    return normalize_handle(merchant.instagram) if merchant else ""


def tagged_merchant_ids(handles: list[str], session: Session) -> set[int]:
    """Ids of the merchants whose Instagram handle appears in `handles`.

    Normalisation happens in Python rather than SQL because `Merchant.instagram`
    is stored as the brand typed it — case and a stray leading @ included — so
    an equality match in the query would miss real brands. There are tens of
    merchants, not millions; scanning them is cheaper than the bugs.
    """
    if not handles:
        return set()
    wanted = set(handles)
    return {
        m.id for m in session.exec(select(Merchant)).all()
        if m.instagram and normalize_handle(m.instagram) in wanted
    }


def evaluate(mention: Optional[Mention], campaign: Optional[Campaign],
             session: Session) -> str:
    """How a claim's chosen campaign compares with its post's tags."""
    handle = campaign_handle(campaign, session)
    if not handle:
        return UNVERIFIABLE

    handles = decode_handles(mention)
    if handles is None:
        return NO_TAG_DATA
    if handle in handles:
        return MATCHED

    # The chosen brand isn't tagged. That is only a contradiction if the post
    # names some OTHER brand we run deals for. Otherwise the member tagged a
    # shop we don't have on Cirqle, which is a gap in our catalogue rather than
    # a false claim, and blocking it would punish them for our missing data.
    return MISMATCH if tagged_merchant_ids(handles, session) else NO_BRAND_TAGGED


def campaigns_for_post(mention: Optional[Mention],
                       session: Session) -> tuple[str, list[Campaign]]:
    """(reason, deals) a post may claim — what the upload page's picker shows.

    Narrowed only when we can read the tags AND they name a brand we know. When
    they don't, the member still has to be able to claim, so the full catalogue
    comes back with the reason saying why it wasn't narrowed.

    A narrowed list can legitimately be empty: the brand they tagged runs no
    deals right now. Falling back to everything there would offer deals that
    `evaluate` then rejects at upload, so the empty list stands.
    """
    all_campaigns = list(session.exec(select(Campaign).order_by(Campaign.id)).all())

    handles = decode_handles(mention)
    if handles is None:
        return NO_TAG_DATA, all_campaigns

    ids = tagged_merchant_ids(handles, session)
    if not ids:
        return NO_BRAND_TAGGED, all_campaigns
    return MATCHED, [c for c in all_campaigns if c.merchant_id in ids]
