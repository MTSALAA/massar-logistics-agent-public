"""Business rules of the logistics agent: shipping decision and shipment life cycle.

Decision for one order (commande), evaluated each time the order or a stock level changes:

  statut annulee            -> shipment still in preparation goes back (retour); nothing else to do
  statut expediee/facturee  -> closed (another actor already moved the order on)
  statut en_attente         -> waiting (the order is not confirmed yet, no alert)
  statut confirmee          -> shippable only if the order is valid, the delivery address is known and
                               every line has enough stock; then a shipment is created in 'preparation'
  otherwise                 -> 'blocked' with an alert, or 'held' when the company set wait_for_stock
                               (a held order is re-evaluated each time a stock level arrives)

Address: the commande registry example has no address field, so it is read from optional payload
fields (see docs/Adresse-livraison-proposition.md): `adresse_livraison` or `client.adresse`, text or object,
plus optional `latitude`/`longitude`. Geocoding of text addresses comes in iteration 3.

Stock: `stock_level` objects are matched by payload.sku and summed over warehouses. The agent does not
reserve stock: the Stock agent owns it.
"""
import hashlib
from datetime import date
from typing import NamedTuple

from . import db

CARRIER = "Massar Logistique"

# Allowed moves of a shipment. 'livree' and 'retour' end the life cycle.
TRANSITIONS: dict[str, set[str]] = {
    "preparation": {"en_transit", "retour"},
    "en_transit": {"livree", "retour"},
    "livree": set(),
    "retour": set(),
}
STATUSES = tuple(TRANSITIONS)

ADDRESS_PARTS = ("rue", "adresse", "ligne1", "ligne2", "code_postal", "ville", "wilaya", "pays")


class TransitionError(Exception):
    """An invalid status change. `code` is the HTTP status the operations API answers with."""

    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


class Address(NamedTuple):
    text: str | None
    lat: float | None
    lon: float | None

    @property
    def known(self) -> bool:
        return bool(self.text) or (self.lat is not None and self.lon is not None)


def _number(value, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if low <= value <= high else None


def delivery_address(payload: dict) -> Address:
    """Read the delivery address from the optional fields of a commande payload."""
    client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
    raw = payload.get("adresse_livraison")
    if raw in (None, "", {}):
        raw = client.get("adresse")

    text, lat, lon = None, None, None
    if isinstance(raw, str):
        text = raw.strip() or None
    elif isinstance(raw, dict):
        parts = [str(raw[k]).strip() for k in ADDRESS_PARTS if isinstance(raw.get(k), (str, int)) and str(raw[k]).strip()]
        text = ", ".join(parts) or None
        lat = _number(raw.get("latitude", raw.get("lat")), -90, 90)
        lon = _number(raw.get("longitude", raw.get("lon", raw.get("lng"))), -180, 180)

    if lat is None or lon is None:  # coordinates may also sit at the top of the payload
        lat = _number(payload.get("latitude"), -90, 90)
        lon = _number(payload.get("longitude"), -180, 180)
    if lat is None or lon is None:
        lat = lon = None
    return Address(text, lat, lon)


def order_lines(payload: dict) -> list[tuple[str, float]] | None:
    """[(sku, quantity)] or None when the lines are missing or unusable."""
    lines = payload.get("lignes")
    if not isinstance(lines, list) or not lines:
        return None
    result = []
    for line in lines:
        if not isinstance(line, dict):
            return None
        sku, qty = line.get("sku"), line.get("quantite")
        if not isinstance(sku, str) or not sku or isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty <= 0:
            return None
        result.append((sku, float(qty)))
    return result


def stock_by_sku(tenant_id: str) -> dict[str, float]:
    """Available quantity per sku, summed over warehouses. A sku with no stock_level is absent."""
    totals: dict[str, float] = {}
    for obj in db.list_objects(tenant_id, "stock_level"):
        sku, qty = obj["payload"].get("sku"), obj["payload"].get("quantite_disponible")
        if isinstance(sku, str) and not isinstance(qty, bool) and isinstance(qty, (int, float)):
            totals[sku] = totals.get(sku, 0.0) + float(qty)
    return totals


def _shipment_id(order_ref: str) -> str:
    return f"EXP-{order_ref}"


def _tracking_number(tenant_id: str, order_ref: str) -> str:
    # A readable reference, not a secret: the hash only spreads the numbers out (hence usedforsecurity=False).
    digest = hashlib.sha1(f"{tenant_id}/{order_ref}".encode(), usedforsecurity=False).hexdigest()[:8].upper()
    return f"MSR-{digest}"


def _finish(tenant_id: str, order_ref: str, decision: str, reason: str | None, alert: tuple[str, str] | None):
    """Store the decision; keep only the alert that is still true."""
    db.set_decision(tenant_id, order_ref, decision, reason)
    if alert:
        db.raise_alert(tenant_id, order_ref, alert[0], alert[1])
    db.resolve_alerts(tenant_id, order_ref, keep_kind=alert[0] if alert else None)
    return {"order_ref": order_ref, "decision": decision, "reason": reason}


def evaluate_order(tenant_id: str, order_ref: str) -> dict | None:
    """Apply the decision rules to one order. Returns the decision, or None if the order is unknown."""
    order = db.get_object(tenant_id, "commande", order_ref)
    if order is None:
        return None
    payload = order["payload"]
    statut = payload.get("statut", "confirmee")  # a missing status is treated as confirmed
    existing = db.get_shipment_by_order(tenant_id, order_ref)

    if statut == "annulee":
        if existing and existing["status"] == "preparation":
            change_status(tenant_id, existing["shipment_id"], "retour", "Order cancelled before pick-up")
        return _finish(tenant_id, order_ref, "cancelled", "Order cancelled", None)
    if statut in ("expediee", "facturee"):
        return _finish(tenant_id, order_ref, "closed", f"Order already {statut}", None)
    if statut == "en_attente":
        return _finish(tenant_id, order_ref, "waiting", "Order not confirmed yet", None)
    if existing:
        return _finish(tenant_id, order_ref, "shipped", existing["shipment_id"], None)

    lines = order_lines(payload)
    if lines is None:
        return _finish(
            tenant_id, order_ref, "blocked", "Order lines missing or invalid",
            ("order_invalid", "The order has no usable lines (sku and positive quantity are required)."),
        )

    address = delivery_address(payload)
    if not address.known:
        return _finish(
            tenant_id, order_ref, "blocked", "Delivery address missing",
            ("address_missing", "No delivery address: send adresse_livraison, client.adresse or coordinates."),
        )

    stock = stock_by_sku(tenant_id)
    problems = []
    for sku, qty in lines:
        if sku not in stock:
            problems.append(f"{sku}: no stock information")
        elif stock[sku] < qty:
            problems.append(f"{sku}: needs {qty:g}, available {stock[sku]:g}")
    if problems:
        kind = "stock_unknown" if all("no stock information" in p for p in problems) else "stock_insufficient"
        message = "Not enough stock - " + "; ".join(problems)
        if (db.get_config(tenant_id) or {}).get("wait_for_stock", False):
            return _finish(tenant_id, order_ref, "held", message, (kind, message))
        return _finish(tenant_id, order_ref, "blocked", message, (kind, message))

    # create_shipment ignores a duplicate (two notifications at once): the shipment then exists already.
    db.create_shipment(
        tenant_id,
        {
            "shipment_id": _shipment_id(order_ref),
            "order_ref": order_ref,
            "status": "preparation",
            "carrier": CARRIER,
            "tracking_number": _tracking_number(tenant_id, order_ref),
            "address": address.text,
            "lat": address.lat,
            "lon": address.lon,
            "note": "Shipment created: stock available and address known",
        },
    )
    return _finish(tenant_id, order_ref, "shipped", _shipment_id(order_ref), None)


def reevaluate_held(tenant_id: str) -> list[dict]:
    """A stock level changed: give every held order another chance."""
    results = []
    for held in db.list_decisions(tenant_id, "held"):
        result = evaluate_order(tenant_id, held["order_ref"])
        if result:
            results.append(result)
    return results


def ingest(tenant_id: str, object_type: str, external_id: str, version: int, payload: dict) -> dict:
    """Store an object received from the core (push or pull) and apply the rules it triggers."""
    previous = db.get_object(tenant_id, object_type, external_id)
    if previous and version < previous["version"]:
        return {"processed": False, "stale": True}  # an older version arrived late: ignore it
    db.save_object(tenant_id, object_type, external_id, version, payload)
    if object_type == "commande":
        decision = evaluate_order(tenant_id, external_id)
        return {"processed": True, "decision": decision["decision"] if decision else None}
    if object_type == "stock_level":
        reevaluate_held(tenant_id)
    return {"processed": True}


# ---------------------------------------------------------------------------
# Shipment life cycle
# ---------------------------------------------------------------------------
def change_status(tenant_id: str, shipment_id: str, new_status: str, reason: str | None = None) -> dict:
    """Move a shipment along its life cycle, keeping a timestamped history."""
    shipment = db.get_shipment(tenant_id, shipment_id)
    if shipment is None:
        raise TransitionError("Shipment not found", 404)
    if new_status not in STATUSES:
        raise TransitionError(f"Unknown status '{new_status}' (expected one of {', '.join(STATUSES)})", 422)
    current = shipment["status"]
    if new_status not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(TRANSITIONS[current])) or "none"
        raise TransitionError(f"Cannot go from '{current}' to '{new_status}' (allowed: {allowed})", 409)
    reason = (reason or "").strip() or None
    if new_status == "retour" and not reason:
        raise TransitionError("A reason is required when a shipment is returned", 422)
    failure = reason if new_status == "retour" else None  # a free note on other moves is not a failure
    db.change_shipment_status(tenant_id, shipment_id, new_status, failure, note=reason)
    if new_status == "livree":
        db.resolve_alerts(tenant_id, shipment["order_ref"])  # delay or failure alerts are over
    elif new_status == "retour" and current == "en_transit":  # it left and came back: a failed delivery
        db.raise_alert(
            tenant_id, shipment["order_ref"], "delivery_failed", f"Delivery of {shipment_id} failed: {reason}"
        )
    return db.get_shipment(tenant_id, shipment_id)


def fail_delivery(tenant_id: str, shipment_id: str, reason: str | None, reschedule_date: str | None = None) -> dict:
    """The shipment comes back (reason required). With a date, a new shipment is created for that day.

    Returns {"shipment": the returned one, "rescheduled": the new one or None}.
    """
    when = None
    if reschedule_date:
        try:
            when = date.fromisoformat(reschedule_date).isoformat()
        except ValueError:
            raise TransitionError("reschedule_date must be an ISO date such as 2026-11-09", 422) from None
    failed = change_status(tenant_id, shipment_id, "retour", reason)
    if when is None:
        return {"shipment": failed, "rescheduled": None}
    order_ref = failed["order_ref"]
    attempt = db.count_shipments_of_order(tenant_id, order_ref)  # 1 for the original, so the first retry is R1
    new_id = f"{_shipment_id(order_ref)}-R{attempt}"
    db.create_shipment(
        tenant_id,
        {
            "shipment_id": new_id, "order_ref": order_ref, "status": "preparation", "carrier": CARRIER,
            "tracking_number": _tracking_number(tenant_id, f"{order_ref}-R{attempt}"),
            "address": failed["address"], "lat": failed["lat"], "lon": failed["lon"], "earliest_date": when,
            "note": f"Rescheduled after the failed delivery of {shipment_id} for {when}",
        },
    )
    return {"shipment": failed, "rescheduled": db.get_shipment(tenant_id, new_id)}
