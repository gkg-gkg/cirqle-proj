"""Merchant reference-receipt upload (live HTTP via TestClient) and the
submit_campaign gate that depends on it. Claude extraction always mocked."""
import io

import pytest
from PIL import Image

from app import receipt_verification as rv
from app.models import Merchant
from app.security import create_merchant_token

VALID_EXTRACTION = {
    "store_name": "Acme Store", "address": "1 High St", "phone": "01234 567890",
    "vat_number": "GB123456789", "format_notes": "Blue header, centered logo.",
    "confidence": 90,
}


def _png_upload(color=(10, 20, 30)):
    img = Image.new("RGB", (32, 32), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _merchant(session, **overrides):
    m = Merchant(email="m@brand.com", password_hash="x", business_name="Acme",
                subscription_status="active", **overrides)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def test_upload_happy_path_sets_ready(client, session, monkeypatch):
    merchant = _merchant(session)
    token = create_merchant_token(merchant)
    monkeypatch.setattr("app.routers.merchant.extract_reference_fields",
                        lambda image_bytes: rv.ReferenceExtraction.model_validate(VALID_EXTRACTION))

    resp = client.post(
        "/merchant/reference-receipt",
        headers={"Authorization": f"Bearer {token}"},
        files={"image": ("ref.png", _png_upload(), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["referenceStatus"] == "ready"
    assert body["referenceFields"]["store_name"] == "Acme Store"

    get_resp = client.get("/merchant/reference-receipt", headers={"Authorization": f"Bearer {token}"})
    assert get_resp.status_code == 200
    assert get_resp.json()["referenceStatus"] == "ready"


def test_low_confidence_extraction_needs_manual_fix(client, session, monkeypatch):
    merchant = _merchant(session)
    token = create_merchant_token(merchant)
    low_confidence = dict(VALID_EXTRACTION, confidence=20)
    monkeypatch.setattr("app.routers.merchant.extract_reference_fields",
                        lambda image_bytes: rv.ReferenceExtraction.model_validate(low_confidence))

    resp = client.post(
        "/merchant/reference-receipt",
        headers={"Authorization": f"Bearer {token}"},
        files={"image": ("ref.png", _png_upload(), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["referenceStatus"] == "needs_manual_fix"


def test_extraction_failure_still_stores_the_image_as_needs_manual_fix(client, session, monkeypatch):
    merchant = _merchant(session)
    token = create_merchant_token(merchant)

    def boom(image_bytes):
        raise rv.VerificationCallError("simulated failure")

    monkeypatch.setattr("app.routers.merchant.extract_reference_fields", boom)

    resp = client.post(
        "/merchant/reference-receipt",
        headers={"Authorization": f"Bearer {token}"},
        files={"image": ("ref.png", _png_upload(), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["referenceStatus"] == "needs_manual_fix"
    assert body["referenceFields"] is None


def test_submit_campaign_blocked_until_reference_is_ready(client, session, monkeypatch):
    merchant = _merchant(session)
    token = create_merchant_token(merchant)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"cardTitle": "New Deal", "cardDesc": "", "longDesc": "", "category": "",
              "rate": 10, "earn": "£10.00", "spendDesc": "", "expiry": "", "brandUrl": "", "terms": ""}

    resp = client.post("/merchant/campaigns", json=payload, headers=headers)
    assert resp.status_code == 409
    assert "reference receipt" in resp.json()["detail"].lower()

    monkeypatch.setattr("app.routers.merchant.extract_reference_fields",
                        lambda image_bytes: rv.ReferenceExtraction.model_validate(VALID_EXTRACTION))
    up = client.post("/merchant/reference-receipt", headers=headers,
                     files={"image": ("ref.png", _png_upload(), "image/png")})
    assert up.status_code == 200
    assert up.json()["referenceStatus"] == "ready"

    resp2 = client.post("/merchant/campaigns", json=payload, headers=headers)
    assert resp2.status_code == 201, resp2.text
