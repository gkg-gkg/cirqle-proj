"""Store locations on campaigns and the postcode lookup. postcodes.io is
mocked — tests never touch the network."""
import json

import pytest

from app import geo
from app.routers.campaigns import ADMIN_KEY

H = {"X-Admin-Key": ADMIN_KEY}
FAKE = {"SW1A 1AA": (51.50101, -0.14159), "M1": (53.48, -2.2362)}


@pytest.fixture(autouse=True)
def fake_lookup(monkeypatch):
    def lookup(raw):
        pc = geo.normalize_postcode(raw)
        if pc not in FAKE:
            raise geo.GeoError(f"We couldn't find the postcode {pc}.")
        lat, lng = FAKE[pc]
        return {"postcode": pc, "lat": lat, "lng": lng}
    monkeypatch.setattr(geo, "lookup_postcode", lookup)


@pytest.mark.parametrize("raw,want", [("sw1a1aa", "SW1A 1AA"), (" SW1A 1AA ", "SW1A 1AA"), ("m1", "M1"), ("EC1A", "EC1A")])
def test_normalize_postcode(raw, want):
    assert geo.normalize_postcode(raw) == want


@pytest.mark.parametrize("raw", ["", "hello", "12345", "SW1A 1AAA"])
def test_normalize_rejects_non_postcodes(raw):
    with pytest.raises(geo.GeoError):
        geo.normalize_postcode(raw)


def _create(client, **fields):
    r = client.post("/campaigns", headers=H, data={"payload": json.dumps({"brand": "Test", "title": "T", **fields})})
    assert r.status_code == 201, r.text
    return r.json()


def test_new_campaign_has_no_stores(client):
    assert _create(client)["stores"] == []


def test_stores_are_geocoded_on_save(client):
    c = _create(client, stores=[{"name": "Westminster", "postcode": "sw1a1aa"}, {"postcode": "M1"}])
    assert c["stores"] == [
        {"name": "Westminster", "postcode": "SW1A 1AA", "lat": 51.50101, "lng": -0.14159},
        {"name": "", "postcode": "M1", "lat": 53.48, "lng": -2.2362},
    ]
    assert client.get(f"/campaigns/{c['id']}").json()["stores"][0]["postcode"] == "SW1A 1AA"


def test_unknown_postcode_refuses_the_save(client):
    r = client.post("/campaigns", headers=H, data={"payload": json.dumps({"brand": "X", "title": "T", "stores": [{"postcode": "ZZ9 9ZZ"}]})})
    assert r.status_code == 422 and "ZZ9 9ZZ" in r.json()["detail"]


def test_patch_without_stores_keeps_them(client):
    c = _create(client, stores=[{"postcode": "M1"}])
    r = client.patch(f"/campaigns/{c['id']}", headers=H, data={"payload": json.dumps({"rate": 5})})
    assert r.json()["stores"][0]["postcode"] == "M1"


def test_patch_with_empty_list_clears_stores(client):
    c = _create(client, stores=[{"postcode": "M1"}])
    r = client.patch(f"/campaigns/{c['id']}", headers=H, data={"payload": json.dumps({"stores": []})})
    assert r.json()["stores"] == []


def test_geo_postcode_endpoint(client):
    assert client.get("/geo/postcode", params={"q": "sw1a 1aa"}).json() == {"postcode": "SW1A 1AA", "lat": 51.50101, "lng": -0.14159}
    assert client.get("/geo/postcode", params={"q": "nonsense"}).status_code == 422
