"""Calls from the agent to the platform core (the only outside service this module talks to).

- publish:  POST {core}/exchange/agent/expedition      (our `produces`)
- pull:     GET  {core}/exchange/agent/{commande|stock_level}   (our `consumes`; fallback to the push)

The core address comes from the CORE_URL environment variable. When it is not set the agent works
offline: shipments stay "pending publication" and are sent later. The tenant API key is only ever put
in the Authorization header; it is never logged.
"""
import logging
import os

import httpx

from . import db, logistics

log = logging.getLogger("massar.core")

TIMEOUT_SECONDS = 5  # the core itself gives up on an agent after 10 s
PULLED_TYPES = ("stock_level", "commande")  # stock first, so a pulled order sees the stock it needs

transport: httpx.BaseTransport | None = None  # tests plug an httpx.MockTransport here


def core_url() -> str | None:
    return (os.getenv("CORE_URL") or "").rstrip("/") or None


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=TIMEOUT_SECONDS, transport=transport)


def _headers(tenant: dict) -> dict:
    return {"Authorization": f"Bearer {tenant['api_key']}", "X-Tenant-Id": tenant["tenant_id"]}


def expedition_object(shipment: dict) -> dict:
    """The `expedition` of the data contract registry (unknown fields would be ignored by readers)."""
    return {
        "external_id": shipment["shipment_id"],
        "payload": {
            "numero_expedition": shipment["shipment_id"],
            "reference_commande": shipment["order_ref"],
            "transporteur": shipment["carrier"],
            "numero_suivi": shipment["tracking_number"],
            "statut": shipment["status"],
        },
    }


def publish_pending(tenant_id: str) -> dict:
    """Send every shipment whose latest state the core does not have yet. Never raises."""
    base = core_url()
    tenant = db.get_tenant(tenant_id)
    if base is None or tenant is None:
        return {"published": 0, "failed": 0, "skipped": True}
    published = failed = 0
    try:
        with _client(base) as client:
            for shipment in db.list_pending_publication(tenant_id):
                try:
                    response = client.post(
                        "/exchange/agent/expedition", headers=_headers(tenant), json=expedition_object(shipment)
                    )
                    response.raise_for_status()
                    version = (response.json().get("data") or {}).get("version")
                    db.mark_published(tenant_id, shipment["shipment_id"], version, shipment["updated_at"])
                    published += 1
                except (httpx.HTTPError, ValueError) as exc:
                    failed += 1
                    log.warning("Could not publish %s: %s", shipment["shipment_id"], type(exc).__name__)
    except httpx.HTTPError as exc:  # client could not even be used
        log.warning("Core unreachable: %s", type(exc).__name__)
    return {"published": published, "failed": failed, "skipped": False}


def pull_from_core(tenant_id: str) -> dict:
    """Read the current commande and stock_level objects from the core and apply the rules. Never raises."""
    base = core_url()
    tenant = db.get_tenant(tenant_id)
    if base is None or tenant is None:
        return {"pulled": 0, "skipped": True}
    pulled = 0
    try:
        with _client(base) as client:
            for object_type in PULLED_TYPES:
                try:
                    response = client.get(f"/exchange/agent/{object_type}", headers=_headers(tenant))
                    response.raise_for_status()
                    items = response.json().get("data") or []
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning("Could not pull %s: %s", object_type, type(exc).__name__)
                    continue
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("payload"), dict) and item.get("external_id"):
                        logistics.ingest(
                            tenant_id, object_type, str(item["external_id"]), int(item.get("version") or 1), item["payload"]
                        )
                        pulled += 1
    except httpx.HTTPError as exc:
        log.warning("Core unreachable: %s", type(exc).__name__)
    return {"pulled": pulled, "skipped": False}


# How serious each alert is for the core's event log (info | low | medium | high | critical).
SEVERITY = {
    "delivery_failed": "high", "delay": "medium", "unplanned": "medium", "stock_insufficient": "medium",
    "stock_unknown": "low", "location_suspect": "low", "address_missing": "low", "address_not_found": "low", "address_not_located": "low",
    "order_invalid": "low",
}


def send_pending_alerts(tenant_id: str) -> dict:
    """Tell the core about open alerts it has not seen (POST /webhooks/agent-events). Never raises."""
    base = core_url()
    tenant = db.get_tenant(tenant_id)
    if base is None or tenant is None:
        return {"sent": 0, "failed": 0, "skipped": True}
    sent = failed = 0
    try:
        with _client(base) as client:
            for alert in db.list_unnotified_alerts(tenant_id):
                body = {
                    "event_type": alert["kind"],
                    "severity": SEVERITY.get(alert["kind"], "low"),
                    "data": {"order_ref": alert["order_ref"], "message": alert["message"]},
                }
                try:
                    client.post("/webhooks/agent-events", headers=_headers(tenant), json=body).raise_for_status()
                    db.mark_alert_notified(tenant_id, alert["order_ref"], alert["kind"])
                    sent += 1
                except httpx.HTTPError as exc:
                    failed += 1
                    log.warning("Could not send alert %s: %s", alert["kind"], type(exc).__name__)
    except httpx.HTTPError as exc:
        log.warning("Core unreachable: %s", type(exc).__name__)
    return {"sent": sent, "failed": failed, "skipped": False}


def after_change(tenant_id: str) -> None:
    """Everything the core should hear after something changed: shipments first, then alerts."""
    publish_pending(tenant_id)
    send_pending_alerts(tenant_id)


def sync(tenant_id: str) -> dict:
    """Pull what we may have missed, then send what the core may not have."""
    pulled = pull_from_core(tenant_id)
    published = publish_pending(tenant_id)
    alerts = send_pending_alerts(tenant_id)
    return {"pull": pulled, "publish": published, "alerts": alerts}
