"""Perceptual-hash duplicate detection (app/receipt_verification.py)."""
import io
import random

import pytest
from PIL import Image, ImageDraw

from app.receipt_verification import IMAGE_HASH_THRESHOLD, find_duplicate_hash, image_phash
from app.models import Receipt, User


def _png_bytes(color: tuple) -> bytes:
    img = Image.new("RGB", (64, 64), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _structured_png_bytes(seed: int) -> bytes:
    # pHash is structure-based (DCT of luminance), not color-based -- two flat
    # solid-color images hash identically regardless of color. Use distinct
    # random noise patterns so the two images actually differ structurally.
    rng = random.Random(seed)
    img = Image.new("RGB", (64, 64))
    pixels = img.load()
    for x in range(64):
        for y in range(64):
            pixels[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_identical_images_hash_to_zero_distance():
    a = _png_bytes((200, 50, 50))
    b = _png_bytes((200, 50, 50))
    hash_a, hash_b = image_phash(a), image_phash(b)
    assert hash_a == hash_b


def test_different_images_hash_far_apart():
    first = image_phash(_structured_png_bytes(1))
    second = image_phash(_structured_png_bytes(2))
    from app.receipt_verification import _hamming
    assert _hamming(first, second) > IMAGE_HASH_THRESHOLD


def _user(session):
    u = User(first_name="A", last_name="B", email="a@b.com", password_hash="x")
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_find_duplicate_hash_matches_within_threshold(session):
    user = _user(session)
    h = image_phash(_png_bytes((10, 20, 30)))
    existing = Receipt(user_id=user.id, post_id="p1", brand="Acme", amount=1.0,
                       image_key="k1", image_hash=h)
    session.add(existing)
    session.commit()
    session.refresh(existing)

    new_receipt = Receipt(user_id=user.id, post_id="p2", brand="Acme", amount=1.0,
                          image_key="k2")
    session.add(new_receipt)
    session.commit()
    session.refresh(new_receipt)

    match = find_duplicate_hash(session, h, new_receipt.id)
    assert match is not None
    assert match.id == existing.id


def test_find_duplicate_hash_no_match_when_no_prior_hashes(session):
    user = _user(session)
    receipt = Receipt(user_id=user.id, post_id="p1", brand="Acme", amount=1.0, image_key="k1")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    h = image_phash(_png_bytes((10, 20, 30)))
    assert find_duplicate_hash(session, h, receipt.id) is None


def test_find_duplicate_hash_excludes_self(session):
    user = _user(session)
    h = image_phash(_png_bytes((10, 20, 30)))
    receipt = Receipt(user_id=user.id, post_id="p1", brand="Acme", amount=1.0,
                      image_key="k1", image_hash=h)
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    assert find_duplicate_hash(session, h, receipt.id) is None
