"""Rate limiting: the global per-IP ceiling, admin-key lockout, and the
per-endpoint caps. conftest turns limits off for every other test; these
switch them back on and start from an empty hit table."""
import pytest

from app import ratelimit
from app.routers.campaigns import ADMIN_KEY


@pytest.fixture()
def limits_on(monkeypatch):
    monkeypatch.setenv("CIRQLE_RATE_LIMIT", "on")
    ratelimit._hits.clear()
    yield
    ratelimit._hits.clear()


def test_global_limit_returns_429_with_cors_and_retry_after(client, limits_on, monkeypatch):
    monkeypatch.setattr(ratelimit, "GLOBAL_LIMIT", 5)
    for _ in range(5):
        assert client.get("/campaigns").status_code == 200
    r = client.get("/campaigns", headers={"Origin": "https://cirqle.co.uk"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    # CORS must wrap the limiter, or the browser sees a network error, not a 429.
    assert "access-control-allow-origin" in r.headers


def test_global_limit_is_per_ip(client, limits_on, monkeypatch):
    monkeypatch.setattr(ratelimit, "GLOBAL_LIMIT", 2)
    for _ in range(2):
        client.get("/campaigns", headers={"X-Forwarded-For": "1.1.1.1"})
    assert client.get("/campaigns", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    assert client.get("/campaigns", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200


def test_stripe_webhook_is_exempt_from_global_limit(client, limits_on, monkeypatch):
    monkeypatch.setattr(ratelimit, "GLOBAL_LIMIT", 1)
    client.get("/campaigns")
    assert client.post("/stripe/webhook", content=b"{}").status_code != 429


def test_admin_key_locks_out_after_ten_wrong_guesses(client, limits_on):
    for _ in range(10):
        assert client.get("/admin/activity", headers={"X-Admin-Key": "guess"}).status_code == 401
    assert client.get("/admin/activity", headers={"X-Admin-Key": "guess"}).status_code == 429
    # Locked out even with the right key, or guessing would still pay off.
    assert client.get("/admin/activity", headers={"X-Admin-Key": ADMIN_KEY}).status_code == 429


def test_correct_admin_key_never_counts_towards_lockout(client, limits_on):
    for _ in range(30):
        assert client.get("/admin/activity", headers={"X-Admin-Key": ADMIN_KEY}).status_code == 200


def test_non_ascii_admin_key_is_401_not_500(client, limits_on):
    r = client.get("/admin/activity", headers={"X-Admin-Key": "clé".encode("latin-1")})
    assert r.status_code == 401


def test_events_endpoint_is_capped(client, limits_on):
    body = {"campaignId": 999, "kind": "view"}
    for _ in range(60):
        assert client.post("/events", json=body).status_code == 404
    assert client.post("/events", json=body).status_code == 429


def test_sweep_forgets_idle_ips(limits_on, monkeypatch):
    ratelimit._retry_after("x", "9.9.9.9", 5, 10)
    assert "9.9.9.9" in ratelimit._hits["x"]
    real = ratelimit.time.monotonic()
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: real + 60)
    ratelimit._sweep(ratelimit.time.monotonic())
    assert "9.9.9.9" not in ratelimit._hits["x"]
