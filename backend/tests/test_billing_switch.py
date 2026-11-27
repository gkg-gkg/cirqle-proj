"""BILLING_ENABLED: while it is off (the free onboarding period) a brand with
no membership plan can still submit a deal; switched on, the plan gate returns."""
import pytest

import app.routers.merchant as merchant_router
from app.models import Merchant
from app.security import create_merchant_token


def _no_plan_merchant(session):
    # Profile complete and reference receipt ready, but no plan.
    m = Merchant(email="free@brand.com", password_hash="x", business_name="Acme",
                 website="https://acme.example", categories='["Fashion & Beauty"]',
                 subscription_status="none", reference_status="ready")
    session.add(m); session.commit(); session.refresh(m)
    return {"Authorization": f"Bearer {create_merchant_token(m)}"}


@pytest.mark.parametrize("enabled, ok", [(False, True), (True, False)])
def test_plan_gate_follows_billing_switch(client, session, monkeypatch, enabled, ok):
    monkeypatch.setattr(merchant_router, "BILLING_ENABLED", enabled)
    r = client.post("/merchant/campaigns", headers=_no_plan_merchant(session),
                    json={"cardTitle": "Summer sale"})
    if ok:
        assert r.status_code in (200, 201), r.text
    else:
        assert r.status_code == 409 and "membership plan" in r.json()["detail"]
