"""The short partnership application (first name, work email, phone, how they
heard), approve = login + invite, and the portal's "complete your account"
gate that collects everything the form no longer asks for."""
import pytest
from sqlmodel import select

from app.models import Campaign, Merchant, MerchantApplication
from app.security import create_merchant_token, hash_password

ADMIN = {"X-Admin-Key": "dev-admin-key"}
SHORT = {"firstName": "Jane", "email": "jane@brand.com", "phone": "07700 900123", "heard": "Instagram"}


@pytest.fixture(autouse=True)
def admin_key(monkeypatch):
    monkeypatch.setenv("CIRQLE_ADMIN_KEY", "dev-admin-key")


@pytest.fixture()
def sent(monkeypatch):
    box = []
    import app.mailer as mailer
    monkeypatch.setattr(mailer, "send_email",
                        lambda to, subject, text, html="": box.append({"to": to, "text": text}) or True)
    return box


def test_short_application_is_accepted(client):
    r = client.post("/partners", json=SHORT)
    assert r.status_code == 201
    b = r.json()
    assert (b["firstName"], b["email"], b["phone"], b["heard"], b["status"]) == \
           ("Jane", "jane@brand.com", "07700 900123", "Instagram", "pending")


@pytest.mark.parametrize("missing", ["firstName", "email", "phone"])
def test_short_application_requires_its_fields(client, missing):
    body = {k: v for k, v in SHORT.items() if k != missing}
    if missing == "firstName":
        body["firstName"] = "  "
    assert client.post("/partners", json=body).status_code == 422


def test_question_needs_a_message_but_no_phone(client):
    q = {"firstName": "Sam", "email": "sam@x.com", "kind": "enquiry"}
    assert client.post("/partners", json=q).status_code == 422
    assert client.post("/partners", json={**q, "message": "How much?"}).status_code == 201


def test_approve_creates_login_and_invites_without_publishing_a_deal(client, session, sent):
    app_id = client.post("/partners", json=SHORT).json()["id"]
    r = client.post(f"/partners/{app_id}/approve", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["status"] == "approved" and r.json()["inviteSent"] is True
    m = session.exec(select(Merchant).where(Merchant.email == "jane@brand.com")).one()
    assert m.must_set_password and m.business_name == ""
    assert session.exec(select(Campaign)).all() == []          # nothing published
    assert sent[0]["to"] == "jane@brand.com" and sent[0]["text"].startswith("Hi Jane,")


def test_approve_rejects_questions(client):
    app_id = client.post("/partners", json={"firstName": "Sam", "email": "sam@x.com",
                                            "kind": "enquiry", "message": "?"}).json()["id"]
    assert client.post(f"/partners/{app_id}/approve", headers=ADMIN).status_code == 400


def test_approve_leaves_app_pending_if_login_exists(client, session, sent):
    session.add(Merchant(email="jane@brand.com", password_hash=hash_password("x" * 12)))
    session.commit()
    app_id = client.post("/partners", json=SHORT).json()["id"]
    assert client.post(f"/partners/{app_id}/approve", headers=ADMIN).status_code == 409
    assert session.get(MerchantApplication, app_id).status == "pending"


def test_new_merchant_must_complete_account_before_submitting_a_deal(client, session):
    m = Merchant(email="new@brand.com", password_hash=hash_password("x" * 12))
    session.add(m); session.commit(); session.refresh(m)
    h = {"Authorization": f"Bearer {create_merchant_token(m)}"}
    assert client.get("/merchant/profile", headers=h).json()["profileComplete"] is False
    r = client.post("/merchant/campaigns", headers=h, json={"cardTitle": "Summer sale"})
    assert r.status_code == 400 and "Complete your account" in r.json()["detail"]
    p = client.patch("/merchant/profile", headers=h, json={
        "businessName": "Nova Athletics", "website": "https://nova.example", "categories": ["Fitness"]})
    assert p.json()["profileComplete"] is True


def test_list_marks_which_applications_have_a_login(client, sent):
    a1 = client.post("/partners", json=SHORT).json()["id"]
    a2 = client.post("/partners", json={**SHORT, "email": "other@brand.com"}).json()["id"]
    client.post(f"/partners/{a1}/approve", headers=ADMIN)
    rows = {r["id"]: r["hasLogin"] for r in client.get("/partners", headers=ADMIN).json()}
    assert rows == {a1: True, a2: False}
