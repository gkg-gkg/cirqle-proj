"""One image file can back exactly one claim, and replacing a receipt cleans up."""
import io

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlmodel import select
from starlette.datastructures import Headers, UploadFile

from app import storage
from app.models import Campaign, Mention, Receipt, User
from app.routers.receipts import create_receipt

# Two valid 1x1 PNGs with different bytes. Hard-coded so the suite needs no
# image library — nothing here looks at the pixels.
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082")
PNG_B = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63f8cf00000301010018dd8db00000000049454e44"
    "ae426082")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Write receipts to a temp dir, never backend/receipts/."""
    path = tmp_path / "receipts"
    path.mkdir()
    monkeypatch.setattr(storage, "RECEIPTS_DIR", path)
    return path


@pytest.fixture
def members(session):
    ann = User(email="ann@x.com", first_name="Ann", last_name="A", password_hash="x")
    ben = User(email="ben@x.com", first_name="Ben", last_name="B", password_hash="x")
    session.add(ann)
    session.add(ben)
    session.add(Campaign(id=1, brand="Tesco", earn="£5.00"))
    for post in ("post1", "post2", "post3"):
        session.add(Mention(id=post, user_id=1, timestamp="2026-08-30T10:00:00Z"))
    session.commit()
    session.refresh(ann)
    session.refresh(ben)
    return ann, ben


@pytest.fixture
def submit(session, store):
    def _submit(user, post_id, image):
        upload = UploadFile(file=io.BytesIO(image), size=len(image),
                            filename="receipt.png",
                            headers=Headers({"content-type": "image/png"}))
        return create_receipt(background=BackgroundTasks(), post_id=post_id,
                              campaign_id=1, referred_by_handle=None,
                              image=upload, user=user, session=session)
    return _submit


def test_first_upload_is_accepted(members, submit):
    ann, _ = members
    assert submit(ann, "post1", PNG_A).id is not None


def test_reupload_for_the_same_post_replaces(members, submit, session, store):
    """A member correcting their own claim must still work."""
    ann, _ = members
    first = submit(ann, "post1", PNG_A)
    original_key = session.get(Receipt, first.id).image_key

    second = submit(ann, "post1", PNG_A)
    assert second.id == first.id                     # same claim, not a new one

    assert session.get(Receipt, first.id).image_key != original_key
    # The photo it replaced must not linger in the private bucket: nothing
    # points at it any more, so account deletion would never find it.
    assert not (store / original_key).exists()


def test_same_image_on_another_post_is_blocked(members, submit):
    ann, _ = members
    submit(ann, "post1", PNG_A)
    with pytest.raises(HTTPException) as raised:
        submit(ann, "post2", PNG_A)
    assert raised.value.status_code == 409


def test_same_image_on_another_account_is_blocked(members, submit):
    ann, ben = members
    submit(ann, "post1", PNG_A)
    with pytest.raises(HTTPException) as raised:
        submit(ben, "post3", PNG_A)
    assert raised.value.status_code == 409


def test_a_different_image_is_accepted(members, submit):
    ann, ben = members
    submit(ann, "post1", PNG_A)
    assert submit(ben, "post3", PNG_B).id is not None


def test_rejected_upload_leaves_nothing_behind(members, submit, session, store):
    ann, ben = members
    submit(ann, "post1", PNG_A)
    with pytest.raises(HTTPException):
        submit(ben, "post3", PNG_A)

    live = {r.image_key for r in session.exec(select(Receipt)).all()}
    assert {p.name for p in store.glob("*")} == live
