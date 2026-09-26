"""Postcode lookup for the browse page's "deals near me" (see app/geo.py).

Public, so it's rate limited; the lookup itself goes to postcodes.io from here,
never from the shopper's browser.
"""
from fastapi import APIRouter, HTTPException, Query

from .. import geo
from ..models import GeoOut
from ..ratelimit import rate_limit

router = APIRouter(prefix="/geo", tags=["geo"])


@router.get("/postcode", response_model=GeoOut,
            dependencies=[rate_limit("geo_postcode", limit=30, window=600)])
def postcode(q: str = Query(..., max_length=10)):
    try:
        return geo.lookup_postcode(q)
    except geo.GeoError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except geo.GeoUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
