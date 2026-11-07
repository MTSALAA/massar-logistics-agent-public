"""Distances on the globe and geocoding (text address -> coordinates) with Nominatim.

Nominatim usage policy (https://operations.osmfoundation.org/policies/nominatim/): at most one request
per second, a User-Agent that identifies the application, and results are cached. This module does all
three. The cache is per company (an address is company data, and the purge must delete it).

Geocoding is OFF unless NOMINATIM_URL is set. This is one of the two outside services the agent can
call (the other is OSRM, which runs on the company's own machine); both are listed in docs/.
"""
import logging
import math
import os
import threading
import time
from typing import NamedTuple

import httpx

from . import db

log = logging.getLogger("massar.geo")

EARTH_RADIUS_M = 6_371_000
TIMEOUT_SECONDS = 8

min_interval = 1.1  # seconds between two requests to Nominatim (policy: at most 1 per second)
transport: httpx.BaseTransport | None = None  # tests plug an httpx.MockTransport here

_lock = threading.Lock()
_last_call = 0.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in metres between two points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


class GeoResult(NamedTuple):
    lat: float | None
    lon: float | None
    status: str  # found | cache | not_found | unavailable | disabled

    @property
    def located(self) -> bool:
        return self.lat is not None and self.lon is not None


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def nominatim_url() -> str | None:
    return (os.getenv("NOMINATIM_URL") or "").rstrip("/") or None


def _user_agent() -> str:
    contact = os.getenv("NOMINATIM_CONTACT") or "no-contact-set"
    return f"MassarLogisticsAgent/1.1 ({contact})"


def geocode(tenant_id: str, text: str) -> GeoResult:
    """Coordinates of an address: cache first, then Nominatim. Never raises."""
    key = normalize(text)
    if not key:
        return GeoResult(None, None, "not_found")
    cached = db.geocache_get(tenant_id, key)
    if cached is not None:
        return GeoResult(cached["lat"], cached["lon"], "cache") if cached["found"] else GeoResult(None, None, "not_found")

    base = nominatim_url()
    if base is None:
        return GeoResult(None, None, "disabled")

    global _last_call
    params = {"q": text, "format": "jsonv2", "limit": 1}
    if os.getenv("NOMINATIM_COUNTRY"):
        params["countrycodes"] = os.environ["NOMINATIM_COUNTRY"]
    with _lock:  # one request at a time, spaced by min_interval; the answer is saved before the next thread looks
        again = db.geocache_get(tenant_id, key)  # a thread that held the lock before us may have asked already
        if again is not None:
            return GeoResult(again["lat"], again["lon"], "cache") if again["found"] else GeoResult(None, None, "not_found")
        wait = min_interval - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            with httpx.Client(base_url=base, timeout=TIMEOUT_SECONDS, transport=transport) as client:
                response = client.get("/search", params=params, headers={"User-Agent": _user_agent()})
            response.raise_for_status()
            results = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Geocoding unavailable: %s", type(exc).__name__)
            return GeoResult(None, None, "unavailable")  # not cached: try again later
        finally:
            _last_call = time.monotonic()

        if isinstance(results, list) and results:
            try:
                lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
            except (KeyError, TypeError, ValueError):
                return GeoResult(None, None, "unavailable")
            db.geocache_put(tenant_id, key, lat, lon, True)
            return GeoResult(lat, lon, "found")
        db.geocache_put(tenant_id, key, None, None, False)  # the service answered: the address is unknown
        return GeoResult(None, None, "not_found")
