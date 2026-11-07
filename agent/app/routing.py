"""Distance and duration matrix between points: OSRM on the real road network, or straight lines.

OSRM runs as a separate container (docker compose --profile routing). If OSRM_URL is not set, or OSRM
does not answer, the matrix is computed from straight-line (haversine) distances and an assumed average
speed, and the plan says so (`source: straight_line`). The agent itself stays autonomous.
"""
import logging
import os

import httpx

from .geo import haversine_m

log = logging.getLogger("massar.routing")

TIMEOUT_SECONDS = 5
ROUTE_TIMEOUT_SECONDS = 10  # a road line through many stops takes longer than a distance table
STRAIGHT_LINE_SPEED_KMH = 35  # assumed average speed when there is no road network (urban and suburban mix)
STRAIGHT_LINE_DETOUR = 1.3  # a road is on average about 30 % longer than the straight line between two towns
SNAP_WARNING_M = 500  # a point farther than this from any road is suspicious (sea, field, wrong address)

transport: httpx.BaseTransport | None = None  # tests plug an httpx.MockTransport here


class Matrix:
    """distance_m[i][j] and duration_s[i][j] between points i and j, plus where they come from."""

    def __init__(
        self, distance_m: list[list[int]], duration_s: list[list[int]], source: str, note: str = "",
        snap_m: list[float | None] | None = None,
    ):
        self.distance_m = distance_m
        self.duration_s = duration_s
        self.source = source  # 'osrm' or 'straight_line'
        self.note = note
        # Distance in metres from each input point to the road OSRM attached it to (None without OSRM).
        self.snap_m = snap_m


def osrm_url() -> str | None:
    return (os.getenv("OSRM_URL") or "").rstrip("/") or None


def straight_line_matrix(points: list[tuple[float, float]], note: str = "") -> Matrix:
    speed_ms = STRAIGHT_LINE_SPEED_KMH * 1000 / 3600
    n = len(points)
    dist = [[0] * n for _ in range(n)]
    dur = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                d = haversine_m(points[i][0], points[i][1], points[j][0], points[j][1]) * STRAIGHT_LINE_DETOUR
                dist[i][j] = round(d)
                dur[i][j] = round(d / speed_ms)
    return Matrix(dist, dur, "straight_line", note)


def route_geometry(points: list[tuple[float, float]]) -> list[list[float]] | None:
    """The road line through the points in order, as [[lat, lon], ...], or None without the road network.

    Used to draw a route on the map along the real roads. Never raises.
    """
    base = osrm_url()
    if base is None or len(points) < 2:
        return None
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)  # OSRM wants lon,lat
    try:
        with httpx.Client(base_url=base, timeout=ROUTE_TIMEOUT_SECONDS, transport=transport) as client:
            response = client.get(f"/route/v1/driving/{coords}", params={"overview": "full", "geometries": "geojson", "steps": "false"})
        response.raise_for_status()
        body = response.json()
        if body.get("code") != "Ok":
            raise ValueError(body.get("message") or body.get("code"))
        line = body["routes"][0]["geometry"]["coordinates"]
        return [[round(lat, 6), round(lon, 6)] for lon, lat in line]  # back to lat, lon
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("Could not get the road line: %s", type(exc).__name__)
        return None


def build_matrix(points: list[tuple[float, float]]) -> Matrix:
    """points are (lat, lon); point 0 is the depot. Never raises."""
    base = osrm_url()
    if base is None:
        return straight_line_matrix(points, "OSRM_URL not set")
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)  # OSRM wants lon,lat
    try:
        with httpx.Client(base_url=base, timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.get(f"/table/v1/driving/{coords}", params={"annotations": "duration,distance"})
        response.raise_for_status()
        body = response.json()
        if body.get("code") != "Ok":
            raise ValueError(body.get("message") or body.get("code"))
        durations, distances = body["durations"], body["distances"]
        n = len(points)
        if len(durations) != n or len(distances) != n:
            raise ValueError("unexpected matrix size")
        # OSRM answers null when two points are not connected by road: treat as unreachable (very far).
        far_m, far_s = 10_000_000, 100_000
        dist = [[round(v) if v is not None else far_m for v in row] for row in distances]
        dur = [[round(v) if v is not None else far_s for v in row] for row in durations]
        snap = [s.get("distance") if isinstance(s, dict) else None for s in body.get("sources", [])]
        return Matrix(dist, dur, "osrm", snap_m=snap if len(snap) == n else None)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("OSRM unavailable, using straight lines: %s", type(exc).__name__)
        return straight_line_matrix(points, f"OSRM unavailable ({type(exc).__name__})")
