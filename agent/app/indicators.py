"""Indicators shown on the dashboard, and the delay alerts.

- on-time rate: share of delivered shipments that arrived no later than the planned arrival plus a
  tolerance (15 minutes). Only shipments that were in a plan can be measured. Planned arrivals are
  company local time; delivery times are UTC; the company's `utc_offset_minutes` converts one to the other.
- kilometres per route and the gain against the order-entry sequence come from the stored plans.
- delay alerts: a shipment still waiting or on the road more than the tolerance after its planned arrival.
"""
from datetime import datetime, timedelta, timezone

from . import db

TOLERANCE = timedelta(minutes=15)


def planned_utc(local_iso: str, offset_minutes: int) -> datetime:
    """A planned arrival is a local clock time without zone: shift it back to UTC."""
    return datetime.fromisoformat(local_iso).replace(tzinfo=timezone.utc) - timedelta(minutes=offset_minutes)


def _offset(tenant_id: str) -> int:
    return (db.get_config(tenant_id) or {}).get("utc_offset_minutes", 0)


def compute(tenant_id: str) -> dict:
    shipments = db.list_shipments(tenant_id)
    by_status = {status: 0 for status in ("preparation", "en_transit", "livree", "retour")}
    for s in shipments:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1

    offset, etas = _offset(tenant_id), db.planned_etas(tenant_id)
    measured = on_time = 0
    for shipment_id, delivered_at in db.delivery_times(tenant_id).items():
        if shipment_id in etas:
            measured += 1
            if datetime.fromisoformat(delivered_at) <= planned_utc(etas[shipment_id], offset) + TOLERANCE:
                on_time += 1

    plans = db.list_plans(tenant_id)
    routes = [r for p in plans for r in p.get("routes", [])]
    optimised = sum(p.get("total_distance_m", 0) for p in plans)
    baseline = sum(p.get("baseline_distance_m", 0) for p in plans)
    latest = plans[0] if plans else None
    return {
        "shipments": {"total": len(shipments), **by_status},
        "deliveries": {
            "delivered": by_status["livree"],
            "failed": db.count_failed_deliveries(tenant_id),
            "measured_for_punctuality": measured,
            "on_time": on_time,
            "on_time_rate_pct": round(on_time / measured * 100, 1) if measured else None,
        },
        "routes": {
            "plans": len(plans),
            "routes_planned": len(routes),
            "km_per_route": round(sum(r["distance_m"] for r in routes) / len(routes) / 1000, 1) if routes else None,
            "total_km": round(optimised / 1000, 1),
            "baseline_km": round(baseline / 1000, 1),
            "gain_pct": round((baseline - optimised) / baseline * 100, 1) if baseline else None,
            "latest_plan": latest["plan_id"] if latest else None,
            "latest_gain_pct": latest["gain_pct"] if latest else None,
            "distance_source": latest["source"] if latest else None,
        },
        "open_alerts": len(db.list_alerts(tenant_id)),
    }


def check_delays(tenant_id: str, now: datetime | None = None) -> int:
    """Raise a `delay` alert for each shipment late on its planned arrival. Returns how many are late."""
    now = now or datetime.now(timezone.utc)
    offset, etas, late = _offset(tenant_id), db.planned_etas(tenant_id), 0
    for s in db.list_shipments(tenant_id):
        if s["status"] not in ("preparation", "en_transit") or s["shipment_id"] not in etas:
            continue
        due = planned_utc(etas[s["shipment_id"]], offset)
        if now > due + TOLERANCE:
            late += 1
            minutes = int((now - due).total_seconds() // 60)
            db.raise_alert(
                tenant_id, s["order_ref"], "delay",
                f"{s['shipment_id']} is {minutes} minutes past its planned arrival ({etas[s['shipment_id']]}).",
            )
    return late
