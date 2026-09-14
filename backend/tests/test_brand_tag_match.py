"""Does the post actually tag the brand whose deal it claims?

A post is scraped because it tags @cirqle.co.uk. That makes it a Cirqle post and
says nothing about which BRAND it is about — the member says that separately, by
picking a campaign at upload. Nothing used to check the two agreed, so tagging
one brand and claiming another's deal billed the wrong merchant for a post that
never named them.

The line these tests hold: only a post we positively read, which positively
names a DIFFERENT Cirqle brand, is refused. A post whose tags we never captured,
or which names a shop we don't run deals for, is our missing data rather than
the member's fault, and must still be claimable.
"""
import io
import json
from datetime import date

import pytest
from sqlmodel import Session, select

from app import brandtags
from app.instagram import extract_tagged_handles
from app.models import Campaign, Merchant, Mention, Receipt, User
from app.security import create_token


def _png(seed: str) -> bytes:
    """A distinct byte string per call — the duplicate check hashes the file."""
    return b"\x89PNG\r\n\x1a\n" + seed.encode() + b"\x00" * 32


def _merchant(session: Session, name="Nandos", handle="nandosuk") -> Merchant:
    m = Merchant(email=f"{name.lower()}@example.com", password_hash="x",
                 business_name=name, instagram=handle)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def _campaign(session: Session, merchant=None, brand="Nandos") -> Campaign:
    c = Campaign(brand=brand, card_title="Deal", earn="£10.00",
                 merchant_id=(merchant.id if merchant else None))
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _member(session: Session, n=1) -> User:
    u = User(first_name="M", last_name=str(n), email=f"m{n}@x.com",
             password_hash="x", instagram_handle=f"member{n}",
             status="approved", email_verified_at=date.today())
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _post(session: Session, user: User, post_id="p1", handles=("nandosuk",)) -> Mention:
    """A stored post. `handles=None` is a post scraped before we read tags —
    deliberately different from an empty tuple, which means we read it and
    found nothing."""
    m = Mention(id=post_id, user_id=user.id, owner_username=user.instagram_handle,
                tagged_handles=None if handles is None else json.dumps(list(handles)))
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_token(user)}"}


def _upload(client, user, campaign, *, post_id="p1", seed="a"):
    data = {"campaign_id": str(campaign.id)}
    if post_id:
        data["post_id"] = post_id
    return client.post("/receipts", data=data, headers=_auth(user),
                       files={"image": (f"{seed}.png", io.BytesIO(_png(seed)),
                                        "image/png")})


@pytest.fixture(autouse=True)
def _no_vision(monkeypatch):
    """The receipt-reading check calls out to Claude; out of scope here."""
    monkeypatch.setenv("CIRQLE_RECEIPT_CHECK", "off")


# ── Reading the tags off a scraped post ──────────────────────────────────────

def test_photo_tags_and_caption_mentions_are_both_read():
    """Neither source is complete alone: photo tags never appear in the caption
    text, and an @mention in the caption isn't a photo tag."""
    post = {"taggedUsers": [{"username": "NandosUK"}],
            "caption": "amazing lunch @gregg_s thanks @cirqle.co.uk"}
    assert extract_tagged_handles(post) == ["nandosuk", "gregg_s"]


def test_our_own_handle_is_never_a_brand_tag():
    """Every post in a mentions scrape tags us — that's why it was scraped."""
    assert extract_tagged_handles({"caption": "@cirqle.co.uk"}) == []


def test_tagged_users_may_arrive_as_bare_strings():
    """Apify has shipped this field both ways across actor versions."""
    assert extract_tagged_handles({"taggedUsers": ["NandosUK"]}) == ["nandosuk"]


def test_a_handle_is_not_swallowed_by_sentence_punctuation():
    assert extract_tagged_handles({"caption": "went to @nandosuk."}) == ["nandosuk"]


def test_the_same_brand_tagged_twice_is_recorded_once():
    post = {"taggedUsers": [{"username": "nandosuk"}], "caption": "@nandosuk !"}
    assert extract_tagged_handles(post) == ["nandosuk"]


def test_a_post_with_no_tags_reads_as_empty_not_unknown():
    """[] is evidence (we looked, nothing there); None would mean we never did."""
    assert extract_tagged_handles({"caption": "lovely dinner"}) == []


# ── The check at upload: the one that actually counts ─────────────────────────

def test_claiming_the_brand_you_tagged_is_accepted(session, client):
    nandos = _merchant(session)
    deal = _campaign(session, nandos)
    user = _member(session)
    _post(session, user, handles=["nandosuk"])

    assert _upload(client, user, deal).status_code == 201
    receipt = session.exec(select(Receipt)).one()
    assert receipt.tag_match == brandtags.MATCHED


def test_claiming_a_different_brands_deal_is_refused(session, client):
    """The hole this closes: tag Nando's, claim Greggs, and Greggs is billed for
    a post that never mentioned them."""
    nandos = _merchant(session)
    greggs = _merchant(session, "Greggs", "greggsofficial")
    greggs_deal = _campaign(session, greggs, brand="Greggs")
    user = _member(session)
    _post(session, user, handles=["nandosuk"])

    res = _upload(client, user, greggs_deal)
    assert res.status_code == 422
    assert "different brand" in res.json()["detail"]
    # Nothing was written, and no orphaned upload was left behind.
    assert session.exec(select(Receipt)).all() == []


def test_a_brand_we_dont_run_deals_for_still_lets_the_claim_through(session, client):
    """The member tagged a shop that isn't on Cirqle. That's a gap in our
    catalogue, not a false claim — it's flagged for the reviewer, not blocked."""
    greggs = _merchant(session, "Greggs", "greggsofficial")
    greggs_deal = _campaign(session, greggs, brand="Greggs")
    user = _member(session)
    _post(session, user, handles=["some_independent_cafe"])

    assert _upload(client, user, greggs_deal).status_code == 201
    assert session.exec(select(Receipt)).one().tag_match == brandtags.NO_BRAND_TAGGED


def test_a_post_whose_tags_we_never_captured_is_not_held_against_it(session, client):
    """Every post scraped before this existed has NULL tags. Blocking those
    would have broken every member's claims the day it shipped."""
    nandos = _merchant(session)
    deal = _campaign(session, nandos)
    user = _member(session)
    _post(session, user, handles=None)

    assert _upload(client, user, deal).status_code == 201
    assert session.exec(select(Receipt)).one().tag_match == brandtags.NO_TAG_DATA


def test_a_deal_with_no_brand_behind_it_cannot_be_checked(session, client):
    """Admin-authored campaigns have no merchant, so there's no handle to
    compare a tag against."""
    user = _member(session)
    deal = _campaign(session, None)
    _post(session, user, handles=["nandosuk"])

    assert _upload(client, user, deal).status_code == 201
    assert session.exec(select(Receipt)).one().tag_match == brandtags.UNVERIFIABLE


def test_a_brand_with_no_handle_on_file_cannot_be_checked(session, client):
    merchant = _merchant(session, "Nandos", handle="")
    deal = _campaign(session, merchant)
    user = _member(session)
    _post(session, user, handles=["nandosuk"])

    assert _upload(client, user, deal).status_code == 201
    assert session.exec(select(Receipt)).one().tag_match == brandtags.UNVERIFIABLE


def test_a_handle_stored_with_an_at_sign_still_matches(session, client):
    """Merchants type their handle themselves — leading @, stray capitals and
    all. Comparing it raw would miss the brand they actually tagged."""
    nandos = _merchant(session, handle="@NandosUK")
    deal = _campaign(session, nandos)
    user = _member(session)
    _post(session, user, handles=["nandosuk"])

    assert _upload(client, user, deal).status_code == 201
    assert session.exec(select(Receipt)).one().tag_match == brandtags.MATCHED


def test_a_visit_claim_has_no_post_so_nothing_to_check(session, client):
    nandos = _merchant(session)
    deal = _campaign(session, nandos)
    deal.visits_enabled, deal.visit_earn = True, 2.0
    session.add(deal)
    session.commit()
    user = _member(session)

    assert _upload(client, user, deal, post_id="").status_code == 201
    assert session.exec(select(Receipt)).one().tag_match == ""


def test_reuploading_over_an_old_claim_restamps_the_verdict(session, client):
    """A re-upload replaces the row in place. If it kept the old verdict, a
    member could re-upload against a post whose tags now say something else and
    the reviewer would read a stale answer."""
    nandos = _merchant(session)
    deal = _campaign(session, nandos)
    user = _member(session)
    post = _post(session, user, handles=None)

    assert _upload(client, user, deal, seed="one").status_code == 201
    assert session.exec(select(Receipt)).one().tag_match == brandtags.NO_TAG_DATA

    post.tagged_handles = json.dumps(["nandosuk"])
    session.add(post)
    session.commit()

    assert _upload(client, user, deal, seed="two").status_code == 201
    rows = session.exec(select(Receipt)).all()
    assert len(rows) == 1 and rows[0].tag_match == brandtags.MATCHED


# ── The deal picker on the upload page ───────────────────────────────────────

def test_the_picker_narrows_to_the_brand_the_post_tags(session, client):
    nandos = _merchant(session)
    greggs = _merchant(session, "Greggs", "greggsofficial")
    nandos_deal = _campaign(session, nandos)
    _campaign(session, greggs, brand="Greggs")
    user = _member(session)
    _post(session, user, handles=["nandosuk"])

    res = client.get("/feed/posts/p1/campaigns", headers=_auth(user))
    assert res.status_code == 200
    body = res.json()
    assert body["reason"] == brandtags.MATCHED
    assert body["taggedHandles"] == ["nandosuk"]
    assert [c["id"] for c in body["campaigns"]] == [nandos_deal.id]


def test_the_picker_falls_back_to_everything_when_it_cannot_narrow(session, client):
    """The member still has to be able to claim — the upload check is what
    stops a wrong pick, not the length of this list."""
    nandos = _merchant(session)
    _campaign(session, nandos)
    user = _member(session)
    _post(session, user, handles=None)

    body = client.get("/feed/posts/p1/campaigns", headers=_auth(user)).json()
    assert body["reason"] == brandtags.NO_TAG_DATA
    assert len(body["campaigns"]) == 1


def test_the_picker_can_come_back_empty(session, client):
    """The tagged brand runs no deals. Offering the rest would just hand the
    member options that upload then refuses."""
    _merchant(session)
    greggs = _merchant(session, "Greggs", "greggsofficial")
    _campaign(session, greggs, brand="Greggs")
    user = _member(session)
    _post(session, user, handles=["nandosuk"])

    body = client.get("/feed/posts/p1/campaigns", headers=_auth(user)).json()
    assert body["reason"] == brandtags.MATCHED
    assert body["campaigns"] == []


def test_you_cannot_read_the_deals_for_someone_elses_post(session, client):
    nandos = _merchant(session)
    _campaign(session, nandos)
    owner = _member(session, 1)
    stranger = _member(session, 2)
    _post(session, owner, handles=["nandosuk"])

    res = client.get("/feed/posts/p1/campaigns", headers=_auth(stranger))
    assert res.status_code == 404


def test_an_unknown_post_is_a_404(session, client):
    user = _member(session)
    assert client.get("/feed/posts/nope/campaigns",
                      headers=_auth(user)).status_code == 404


# ── Capturing the tags in the first place ────────────────────────────────────

def test_a_feed_refresh_stores_the_brands_a_post_tags(session, client, monkeypatch):
    """The wiring the whole feature rests on: the tags only exist in the raw
    Apify item, which is discarded once the post is stored."""
    import app.routers.feed as feed

    monkeypatch.setattr(feed, "scrape_brand_mentions", lambda limit=50: [{
        "id": "p9",
        "ownerUsername": "member1",
        "caption": "lunch @cirqle.co.uk",
        "taggedUsers": [{"username": "NandosUK"}],
    }])
    monkeypatch.setattr(feed, "scrape_profile_stats", lambda handle: None)
    monkeypatch.setattr(feed, "mirror_display_image", lambda url: url)

    user = _member(session)
    assert client.post("/feed/refresh", headers=_auth(user)).status_code == 200

    stored = session.get(Mention, "p9")
    # Our own handle dropped, the brand's kept — and stored as read, not NULL.
    assert json.loads(stored.tagged_handles) == ["nandosuk"]
