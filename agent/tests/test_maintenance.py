"""Housekeeping: delay alerts and retries towards the core happen without anyone opening a page."""
import threading

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    for name in ("CORE_URL", "NOMINATIM_URL", "OSRM_URL", "MAINTENANCE_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    from app import core_client, db, logistics
    from app.config import parse_config

    for tenant in ("A", "B"):
        db.upsert_tenant(tenant, "key-" + tenant, parse_config({}))
        logistics.ingest(tenant, "stock_level", "S", 1, {"sku": "SKU", "quantite_disponible": 100})
        logistics.ingest(tenant, "commande", "O1", 1, {
            "statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 1}], "latitude": 36.7, "longitude": 5.0})
    return db, logistics, core_client, monkeypatch


class FakeCore:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"status": "ok", "data": {"id": "x", "version": 1, "event_id": "e"}, "error": None})

    def paths(self):
        return [(r.url.path, r.headers["X-Tenant-Id"]) for r in self.requests]


def plan_in_the_past(db, tenant="A"):
    stops = [{"vehicle_id": "V1", "seq": 1, "shipment_id": "EXP-O1", "order_ref": "O1", "address": None, "lat": 36.7, "lon": 5.0,
              "demand": 1, "eta": "2020-01-01T08:00:00", "leg_distance_m": 1, "leg_duration_s": 1}]
    db.save_plan(tenant, "2020-01-01", {"source": "straight_line", "routes": [], "total_distance_m": 1, "baseline_distance_m": 1, "gain_pct": 0}, stops)


def test_a_pass_raises_delay_alerts_and_sends_everything_pending(world):
    db, logistics, core_client, monkeypatch = world
    from app import maintenance

    core = FakeCore()
    monkeypatch.setenv("CORE_URL", "http://core.test")
    monkeypatch.setattr(core_client, "transport", httpx.MockTransport(core))
    plan_in_the_past(db, "A")
    result = maintenance.run_once()
    assert result == {"checked": 2, "failed": 0, "late": 1}
    assert [a["kind"] for a in db.list_alerts("A")] == ["delay"] and db.list_alerts("B") == []
    paths = core.paths()
    assert ("/exchange/agent/expedition", "A") in paths and ("/exchange/agent/expedition", "B") in paths  # pending shipments went out
    assert ("/webhooks/agent-events", "A") in paths  # the delay alert reached the core
    assert db.get_shipment("A", "EXP-O1")["publish_pending"] == 0
    before = len(core.requests)
    maintenance.run_once()
    assert len(core.requests) == before  # nothing new to say: nothing sent twice


def test_a_company_that_fails_does_not_stop_the_others(world):
    db, logistics, core_client, monkeypatch = world
    from app import indicators, maintenance

    real = indicators.check_delays

    def flaky(tenant_id, *args):
        if tenant_id == "A":
            raise RuntimeError("secret detail")
        return real(tenant_id, *args)

    monkeypatch.setattr(indicators, "check_delays", flaky)
    plan_in_the_past(db, "B")
    result = maintenance.run_once()
    assert result["failed"] == 1 and result["checked"] == 1 and [a["kind"] for a in db.list_alerts("B")] == ["delay"]


def test_without_a_core_the_pass_still_raises_alerts(world):
    db, logistics, core_client, monkeypatch = world
    from app import maintenance

    plan_in_the_past(db, "A")
    assert maintenance.run_once()["late"] == 1 and db.get_shipment("A", "EXP-O1")["publish_pending"] == 1


def test_the_loop_runs_repeatedly_and_stops_when_asked(world):
    db, logistics, core_client, monkeypatch = world
    from app import maintenance

    ran = threading.Semaphore(0)
    monkeypatch.setattr(maintenance, "run_once", lambda: ran.release() or {})
    stop = threading.Event()
    thread = maintenance.start(stop, interval=0.05)
    assert ran.acquire(timeout=3) and ran.acquire(timeout=3)  # it kept running
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_the_loop_survives_a_failing_pass_and_can_be_switched_off(world):
    db, logistics, core_client, monkeypatch = world
    from app import maintenance

    calls = threading.Semaphore(0)

    def explode():
        calls.release()
        raise RuntimeError("boom")

    monkeypatch.setattr(maintenance, "run_once", explode)
    stop = threading.Event()
    thread = maintenance.start(stop, interval=0.05)
    assert calls.acquire(timeout=3) and calls.acquire(timeout=3)  # still going after the first failure
    stop.set()
    thread.join(timeout=3)
    assert maintenance.start(threading.Event(), interval=0) is None
    monkeypatch.setenv("MAINTENANCE_SECONDS", "0")
    assert maintenance.start(threading.Event()) is None
    monkeypatch.setenv("MAINTENANCE_SECONDS", "not a number")
    assert maintenance.interval_seconds() == maintenance.DEFAULT_INTERVAL_SECONDS


def test_the_application_starts_and_stops_the_loop(world):
    from app import maintenance
    from app.main import app

    seen = []
    world[3].setattr(maintenance, "start", lambda stop, interval=None: seen.append(stop) or None)
    with TestClient(app):
        assert len(seen) == 1 and not seen[0].is_set()
    assert seen[0].is_set()  # stopped with the application
