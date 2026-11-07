"""Regular housekeeping, so nothing depends on someone opening a page.

Every few minutes, for every company:
  - look for deliveries running late and raise the delay alerts;
  - send the core what it has not received yet (shipments that could not be published, alerts not yet sent).

One company failing never stops the others, and the loop never raises. The interval is MAINTENANCE_SECONDS
(default 300; 0 switches the loop off).
"""
import logging
import os
import threading

from . import core_client, db, indicators

log = logging.getLogger("massar.maintenance")

DEFAULT_INTERVAL_SECONDS = 300


def interval_seconds() -> float:
    try:
        return float(os.getenv("MAINTENANCE_SECONDS", DEFAULT_INTERVAL_SECONDS))
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


def run_once() -> dict:
    """One pass over every company. Returns how many were checked and how many failed."""
    checked = failed = late = 0
    for tenant_id in db.list_tenant_ids():
        try:
            late += indicators.check_delays(tenant_id)
            core_client.after_change(tenant_id)
            checked += 1
        except Exception as exc:  # noqa: BLE001 - housekeeping must survive anything
            failed += 1
            log.warning("Housekeeping failed for one company: %s", type(exc).__name__)
    return {"checked": checked, "failed": failed, "late": late}


def start(stop: threading.Event, interval: float | None = None) -> threading.Thread | None:
    """Run the housekeeping until `stop` is set. Returns the thread, or None when it is switched off."""
    wait = interval_seconds() if interval is None else interval
    if wait <= 0:
        return None

    def loop():
        while not stop.wait(wait):  # sleeps, and wakes at once when asked to stop
            try:
                run_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("Housekeeping pass failed: %s", type(exc).__name__)

    thread = threading.Thread(target=loop, name="massar-maintenance", daemon=True)
    thread.start()
    return thread
