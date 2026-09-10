"""Integration test of run_claude_verification end-to-end. The Claude call is
always mocked -- no real network/API key needed. Uses a tmp_path-isolated
local receipts directory so nothing touches the real backend/receipts/."""
import io
import json
from datetime import datetime

import pytest
from PIL import Image

from app import receipt_verification as rv
from app import storage
from app.models import Campaign, Merchant, Receipt, User


def _png_bytes(color=(10, 20, 30)) -> bytes:
    # image_phash needs real, decodable image bytes -- arbitrary placeholder
    # bytes (b"new-image-bytes") would just raise inside PIL.Image.open.
    img = Image.new("RGB", (32, 32), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


VALID_RESULT = {
    "ocr_fields": {"store_name": "Acme", "date": "2026-09-01", "total": 42.0,
                   "currency": "GBP", "line_items": ["Widget"]},
    "authenticity": {"score": 0.95, "matched_fields": ["store_name"], "mismatches": [],
                     "reasoning": "Matches the reference."},
    "purchase_match": {"score": 0.9, "product_match": "yes", "amount_plausible": True,
                       "date_plausible": True, "reasoning": "Plausible purchase."},
    "red_flags": [],
}


@pytest.fixture(autouse=True)
def isolated_receipts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RECEIPTS_DIR", tmp_path)


@pytest.fixture
def cashback_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(rv, "trigger_cashback_calculation", lambda receipt_id: calls.append(receipt_id))
    return calls


def _seed(session, *, reference_status="ready", cashback_mode=None):
    user = User(first_name="A", last_name="B", email="a@b.com", password_hash="x")
    session.add(user)
    session.commit()
    session.refresh(user)

    merchant = Merchant(email="m@brand.com", password_hash="x", business_name="Acme",
                       reference_status=reference_status)
    if reference_status == "ready":
        (storage.RECEIPTS_DIR).mkdir(parents=True, exist_ok=True)
        (storage.RECEIPTS_DIR / "ref.jpg").write_bytes(_png_bytes((100,100,100)))
        merchant.reference_receipt_s3_key = "ref.jpg"
    session.add(merchant)
    session.commit()
    session.refresh(merchant)

    campaign = Campaign(brand="Acme", title="Acme deal", earn="£13.00",
                        spend_desc="on a £30 spend", merchant_id=merchant.id)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    (storage.RECEIPTS_DIR).mkdir(parents=True, exist_ok=True)
    (storage.RECEIPTS_DIR / "new.jpg").write_bytes(_png_bytes((10,20,30)))
    receipt = Receipt(user_id=user.id, post_id="p1", campaign_id=campaign.id,
                      brand="Acme", amount=13.0, image_key="new.jpg")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return user, merchant, campaign, receipt


def test_merchant_reference_not_ready_routes_to_review(session, monkeypatch, cashback_spy):
    user, merchant, campaign, receipt = _seed(session, reference_status="pending_extraction")
    rv.run_claude_verification(receipt.id)
    session.refresh(receipt)
    assert receipt.verification_status == "pending_admin_review"
    assert "reference" in receipt.verification_error
    assert cashback_spy == []


def test_no_campaign_linked_routes_to_review_without_crashing(session, monkeypatch, cashback_spy):
    user = User(first_name="A", last_name="B", email="a@b.com", password_hash="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    (storage.RECEIPTS_DIR).mkdir(parents=True, exist_ok=True)
    (storage.RECEIPTS_DIR / "visit.jpg").write_bytes(_png_bytes((50,60,70)))
    receipt = Receipt(user_id=user.id, post_id="", claim_kind="visit", campaign_id=None,
                      brand="", amount=0.0, image_key="visit.jpg")
    session.add(receipt)
    session.commit()
    session.refresh(receipt)

    rv.run_claude_verification(receipt.id)
    session.refresh(receipt)
    assert receipt.verification_status == "pending_admin_review"
    assert cashback_spy == []


def test_duplicate_hash_overrides_a_high_score(session, monkeypatch, cashback_spy):
    user, merchant, campaign, receipt = _seed(session)
    # A prior receipt whose hash will collide with whatever the (mocked)
    # image_phash of "new.jpg" produces.
    same_hash = "8000000000000000"   # valid 16-char hex, matching imagehash's output shape
    monkeypatch.setattr(rv, "image_phash", lambda data: same_hash)
    prior = Receipt(user_id=user.id, post_id="p0", campaign_id=campaign.id,
                    brand="Acme", amount=13.0, image_key="old.jpg", image_hash=same_hash)
    session.add(prior)
    session.commit()
    session.refresh(prior)

    monkeypatch.setattr(rv, "call_claude_verification",
                        lambda *a, **k: rv.ClaudeReceiptVerification.model_validate(VALID_RESULT))
    monkeypatch.setattr(rv, "AUTO_APPROVAL_ENABLED", True)

    rv.run_claude_verification(receipt.id)
    session.refresh(receipt)
    assert receipt.verification_status == "pending_admin_review"
    detail = json.loads(receipt.authenticity_detail)
    assert detail["duplicate_hash_match_receipt_id"] == prior.id
    assert cashback_spy == []


def test_auto_approve_path_sets_both_status_fields_and_fires_cashback_once(
        session, monkeypatch, cashback_spy):
    user, merchant, campaign, receipt = _seed(session)
    monkeypatch.setattr(rv, "call_claude_verification",
                        lambda *a, **k: rv.ClaudeReceiptVerification.model_validate(VALID_RESULT))
    monkeypatch.setattr(rv, "AUTO_APPROVAL_ENABLED", True)
    monkeypatch.setattr(rv, "VERIFICATION_AUTO_APPROVE_THRESHOLD", 0.85)

    rv.run_claude_verification(receipt.id)
    session.refresh(receipt)

    assert receipt.verification_status == "auto_approved"
    # The critical wiring: verification_status alone doesn't gate cashback.
    assert receipt.status == "verified"
    assert receipt.decision_source == "auto"
    assert receipt.decision_at is not None
    assert receipt.overall_score == pytest.approx(0.5 * 0.95 + 0.5 * 0.9)
    assert receipt.basket_total == 42.0            # apply_reading ran
    assert receipt.basket_currency == "GBP"
    assert cashback_spy == [receipt.id]


def test_auto_approval_disabled_routes_to_review_even_with_a_perfect_score(
        session, monkeypatch, cashback_spy):
    user, merchant, campaign, receipt = _seed(session)
    monkeypatch.setattr(rv, "call_claude_verification",
                        lambda *a, **k: rv.ClaudeReceiptVerification.model_validate(VALID_RESULT))
    monkeypatch.setattr(rv, "AUTO_APPROVAL_ENABLED", False)

    rv.run_claude_verification(receipt.id)
    session.refresh(receipt)
    assert receipt.verification_status == "pending_admin_review"
    assert receipt.status == "pending"    # untouched
    assert cashback_spy == []


def test_claude_call_failure_never_resolves_to_auto_approval(session, monkeypatch, cashback_spy):
    user, merchant, campaign, receipt = _seed(session)

    def boom(*a, **k):
        raise rv.VerificationCallError("simulated failure")

    monkeypatch.setattr(rv, "call_claude_verification", boom)
    monkeypatch.setattr(rv, "AUTO_APPROVAL_ENABLED", True)

    rv.run_claude_verification(receipt.id)
    session.refresh(receipt)
    assert receipt.verification_status == "pending_admin_review"
    assert receipt.verification_error is not None
    assert receipt.status == "pending"
    assert cashback_spy == []


def test_unknown_receipt_is_a_no_op(session, cashback_spy):
    rv.run_claude_verification(999999)   # must not raise
    assert cashback_spy == []
