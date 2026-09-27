"""UK postcode -> coordinates, for "deals near me".

Uses postcodes.io: free, no key, built on ONS / Royal Mail open data. Called
only from our server — when an admin saves a deal's stores, and when a shopper
looks up their own postcode through GET /geo/postcode — so a shopper's IP never
reaches a third party. A browser's own GPS fix never comes here at all; the
browse page measures distances on the device.

Accepts a full postcode ("SW1A 1AA") or just the outward half ("SW1A"), which
postcodes.io resolves to that district's centre — plenty for "within 10 miles".
"""
import re

import httpx

_API = "https://api.postcodes.io"
_FULL = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}$")
_OUTWARD = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")


class GeoError(Exception):
    """The postcode is malformed or unknown (message is safe to show a user)."""


class GeoUnavailable(Exception):
    """postcodes.io could not be reached — not the caller's fault."""


def normalize_postcode(raw: str) -> str:
    """'sw1a1aa' -> 'SW1A 1AA'; 'sw1a' -> 'SW1A'. Raises GeoError if it can't be one."""
    pc = re.sub(r"\s+", "", (raw or "").upper())
    if _FULL.match(pc):
        return f"{pc[:-3]} {pc[-3:]}"
    if _OUTWARD.match(pc):
        return pc
    raise GeoError(f"'{raw.strip()}' doesn't look like a UK postcode.")


def lookup_postcode(raw: str) -> dict:
    """Return {"postcode", "lat", "lng"} for a UK postcode or outward code."""
    pc = normalize_postcode(raw)
    path = f"/postcodes/{pc.replace(' ', '')}" if " " in pc else f"/outcodes/{pc}"
    try:
        r = httpx.get(_API + path, timeout=6)
    except httpx.HTTPError as exc:
        raise GeoUnavailable("Postcode lookup is unavailable right now.") from exc
    if r.status_code == 404:
        raise GeoError(f"We couldn't find the postcode {pc}.")
    if r.status_code != 200:
        raise GeoUnavailable("Postcode lookup is unavailable right now.")
    res = r.json().get("result") or {}
    if res.get("latitude") is None or res.get("longitude") is None:
        # Real but unmapped (some Channel Islands / new-build postcodes).
        raise GeoError(f"We couldn't place the postcode {pc} on the map.")
    return {"postcode": pc, "lat": round(res["latitude"], 5), "lng": round(res["longitude"], 5)}
