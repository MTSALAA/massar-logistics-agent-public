"""Route planning for one company: locate the shipments, build the matrix, optimise, store the plan.

This is the glue between the database (shipments, config), the geocoder, the road network (routing.py)
and the solver (planner.py). The solver itself is pure and lives in planner.py.
"""
import math
import threading
from datetime import date, datetime, time, timedelta, timezone

from . import db, geo, planner, routing

# Alerts raised here. They are closed as soon as the order is located or planned.
MAX_PLAN_STOPS = 500  # the distance table grows with the square of the stops; OSRM is configured for 1000 points

ALERT_NOT_FOUND = "address_not_found"
ALERT_NOT_LOCATED = "address_not_located"
ALERT_UNPLANNED = "unplanned"
ALERT_SUSPECT = "location_suspect"

# Why a shipment was left out of a plan, in words (shown to people and in alerts).
REASONS = {
    "no_location": "it has no coordinates yet",
    "exceeds_capacity": "its load is bigger than every vehicle",
    "too_far_for_working_hours": "the round trip alone is longer than the working day",
    "no_room": "no vehicle had enough capacity or time left",
}


class PlanError(Exception):
    """The plan cannot be computed. `code` is the HTTP status the operations API answers with."""

    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


_locating: dict[str, threading.Lock] = {}
_locating_guard = threading.Lock()


def locate_shipments(tenant_id: str) -> dict:
    """Give coordinates to shipments in preparation that only have a text address (cache, then Nominatim).

    Every incoming order starts one of these in the background. They run one after the other for a company,
    so a burst of orders never asks the geocoder the same question many times at once.
    """
    with _locating_guard:
        lock = _locating.setdefault(tenant_id, threading.Lock())
    with lock:
        return _locate_pending(tenant_id)


def _locate_pending(tenant_id: str) -> dict:
    located = failed = 0
    for s in db.list_shipments(tenant_id):
        if s["status"] != "preparation" or s["lat"] is not None or not s["address"]:
            continue
        result = geo.geocode(tenant_id, s["address"])
        if result.located:
            db.set_shipment_location(tenant_id, s["shipment_id"], result.lat, result.lon)
            db.resolve_alerts(tenant_id, s["order_ref"])
            located += 1
            continue
        failed += 1
        if result.status == "not_found":
            db.raise_alert(
                tenant_id, s["order_ref"], ALERT_NOT_FOUND,
                f"The address '{s['address']}' was not found. Correct it in the order or set the location by hand.",
            )
        else:
            reason = "geocoding is not enabled" if result.status == "disabled" else "the geocoder did not answer"
            db.raise_alert(
                tenant_id, s["order_ref"], ALERT_NOT_LOCATED,
                f"No coordinates for '{s['address']}': {reason}. Send latitude/longitude or set the location by hand.",
            )
    return {"located": located, "failed": failed}


def _demand(tenant_id: str, order_ref: str) -> int:
    """Units to deliver for an order: the sum of its line quantities, at least 1."""
    order = db.get_object(tenant_id, "commande", order_ref)
    lines = (order or {}).get("payload", {}).get("lignes")
    total = 0.0
    if isinstance(lines, list):
        for line in lines:
            qty = line.get("quantite") if isinstance(line, dict) else None
            if isinstance(qty, (int, float)) and not isinstance(qty, bool) and qty > 0:
                total += qty
    return max(1, math.ceil(total))


def _requested_date(tenant_id: str, order_ref: str) -> date | None:
    """Optional `date_livraison_souhaitee` (ISO date) of the order."""
    order = db.get_object(tenant_id, "commande", order_ref)
    raw = (order or {}).get("payload", {}).get("date_livraison_souhaitee")
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def _parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def plan_routes(tenant_id: str, plan_date: str | None = None, solver_seconds: float | None = None) -> dict:
    """Plan the routes of every shipment still in preparation. Returns the plan summary (also stored)."""
    config = db.get_config(tenant_id)
    if config is None:
        raise PlanError("Tenant not activated", 404)
    if not config.get("depot"):
        raise PlanError("No depot configured: set `depot` {lat, lon} in the company configuration", 422)
    try:
        day = date.fromisoformat(plan_date) if plan_date else datetime.now(timezone.utc).date()
    except ValueError:
        raise PlanError("plan_date must be an ISO date such as 2026-11-08", 422) from None
    seconds = planner.DEFAULT_SOLVER_SECONDS if solver_seconds is None else solver_seconds
    if not 0 < seconds <= planner.MAX_SOLVER_SECONDS:
        raise PlanError(f"solver_seconds must be between 0 and {planner.MAX_SOLVER_SECONDS}", 422)

    locate_shipments(tenant_id)

    unassigned, deferred, items = [], [], []
    for s in db.list_shipments(tenant_id):  # already in order-entry sequence
        if s["status"] != "preparation":
            continue
        wanted = [d for d in (_requested_date(tenant_id, s["order_ref"]), _parse_day(s.get("earliest_date"))) if d]
        requested = max(wanted) if wanted else None
        if requested and requested > day:
            deferred.append({"shipment_id": s["shipment_id"], "order_ref": s["order_ref"], "requested_date": requested.isoformat()})
        elif s["lat"] is None or s["lon"] is None:
            unassigned.append({"shipment_id": s["shipment_id"], "order_ref": s["order_ref"], "reason": "no_location"})
        else:
            items.append({"shipment": s, "demand": _demand(tenant_id, s["order_ref"])})

    if len(items) > MAX_PLAN_STOPS:
        raise PlanError(
            f"Too many shipments to plan at once ({len(items)}; the limit is {MAX_PLAN_STOPS}). Plan for several days or deliver some first.", 422)

    vehicles = config["vehicles"]
    capacities = [int(v["capacity"]) for v in vehicles]
    work_start, work_end = _minutes(config["working_hours"]["start"]), _minutes(config["working_hours"]["end"])
    horizon_s = (work_end - work_start) * 60
    service_s = round(config["stop_duration_minutes"] * 60)

    depot = (config["depot"]["lat"], config["depot"]["lon"])
    points = [depot] + [(i["shipment"]["lat"], i["shipment"]["lon"]) for i in items]
    matrix = routing.build_matrix(points)

    # With the road network, OSRM says how far each point is from the nearest road: a point far from any
    # road is probably in the sea or a field, or the address is wrong.
    warnings = []
    if matrix.snap_m:
        for k, item in enumerate(items, start=1):
            if (matrix.snap_m[k] or 0) > routing.SNAP_WARNING_M:
                s = item["shipment"]
                warnings.append({"shipment_id": s["shipment_id"], "order_ref": s["order_ref"],
                                 "snap_distance_m": round(matrix.snap_m[k])})

    # Stops no vehicle could ever serve are set aside with a precise reason instead of being dropped silently.
    keep = [0]
    for k, item in enumerate(items, start=1):
        round_trip = matrix.duration_s[0][k] + service_s + matrix.duration_s[k][0]
        if item["demand"] > max(capacities):
            reason = "exceeds_capacity"
        elif round_trip > horizon_s:
            reason = "too_far_for_working_hours"
        else:
            keep.append(k)
            continue
        unassigned.append({"shipment_id": item["shipment"]["shipment_id"], "order_ref": item["shipment"]["order_ref"], "reason": reason})
    sub = routing.Matrix(
        [[matrix.distance_m[a][b] for b in keep] for a in keep],
        [[matrix.duration_s[a][b] for b in keep] for a in keep],
        matrix.source, matrix.note,
    )
    kept_items = [items[k - 1] for k in keep[1:]]
    demands = [0] + [i["demand"] for i in kept_items]

    solved = planner.solve(sub, demands, capacities, service_s, horizon_s, seconds)
    for node in solved["dropped"]:
        s = kept_items[node - 1]["shipment"]
        unassigned.append({"shipment_id": s["shipment_id"], "order_ref": s["order_ref"], "reason": "no_room"})

    served = [node for route in solved["routes"] for node in route]
    optimised = sum(planner.route_distance(sub, r) for r in solved["routes"])
    baseline = planner.baseline_distance(sub, sorted(served), demands, capacities)  # node order = order of entry
    gain = round((baseline - optimised) / baseline * 100, 1) if baseline > 0 else 0.0

    day_start = datetime.combine(day, time(work_start // 60, work_start % 60))
    stops, route_summaries = [], []
    for vehicle, route in zip(vehicles, solved["routes"]):
        if not route:
            continue
        previous, load = 0, 0
        for seq, node in enumerate(route, start=1):
            item = kept_items[node - 1]
            s = item["shipment"]
            load += item["demand"]
            stops.append({
                "vehicle_id": vehicle["id"], "seq": seq, "shipment_id": s["shipment_id"], "order_ref": s["order_ref"],
                "address": s["address"], "lat": s["lat"], "lon": s["lon"], "demand": item["demand"],
                "eta": (day_start + timedelta(seconds=solved["arrivals"][node])).isoformat(timespec="seconds"),
                "leg_distance_m": sub.distance_m[previous][node], "leg_duration_s": sub.duration_s[previous][node],
            })
            previous = node
        finish = solved["arrivals"][previous] + service_s + sub.duration_s[previous][0]
        route_summaries.append({
            "vehicle_id": vehicle["id"], "stops": len(route), "load": load, "capacity": int(vehicle["capacity"]),
            "distance_m": planner.route_distance(sub, route), "duration_s": finish,
            "back_at_depot": (day_start + timedelta(seconds=finish)).isoformat(timespec="seconds"),
        })

    summary = {
        "source": matrix.source, "source_note": matrix.note,
        "solver_seconds": seconds, "compute_seconds": solved["compute_seconds"],
        "stops_planned": len(stops), "vehicles_used": len(route_summaries),
        "total_distance_m": optimised, "baseline_distance_m": baseline, "gain_pct": gain,
        "total_duration_s": sum(r["duration_s"] for r in route_summaries),
        "routes": route_summaries, "unassigned": unassigned, "deferred": deferred, "warnings": warnings,
    }

    for entry in unassigned:  # explain every shipment left out
        entry["explanation"] = REASONS.get(entry["reason"], entry["reason"])
        if entry["reason"] != "no_location":  # that one already has its own alert
            db.raise_alert(
                tenant_id, entry["order_ref"], ALERT_UNPLANNED, f"Not planned: {entry['explanation']}."
            )
    suspect = {w["order_ref"]: w for w in warnings}
    for stop in stops:
        db.resolve_alerts(tenant_id, stop["order_ref"], keep_kind=ALERT_SUSPECT if stop["order_ref"] in suspect else None)
    for w in warnings:
        db.raise_alert(
            tenant_id, w["order_ref"], ALERT_SUSPECT,
            f"The location of {w['shipment_id']} is {w['snap_distance_m']} m from the nearest road "
            "(in the sea or a field, or a wrong address?). Check the address or set the location by hand.",
        )

    if not stops:
        return {**summary, "plan_id": None, "plan_date": day.isoformat(), "stops": []}
    paths = {}
    if matrix.source == "osrm":  # draw the routes along the real roads
        for vehicle, route in zip(vehicles, solved["routes"]):
            if route:
                line = routing.route_geometry([depot] + [(kept_items[n - 1]["shipment"]["lat"], kept_items[n - 1]["shipment"]["lon"]) for n in route] + [depot])
                if line:
                    paths[vehicle["id"]] = line
    summary["road_geometry"] = bool(paths)
    plan_id = db.save_plan(tenant_id, day.isoformat(), summary, stops, paths)
    return db.get_plan(tenant_id, plan_id)
