"""Tracking: indicators, delay and failure alerts, rescheduling, alerts sent to the core, database migration."""
import json
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

DEPOT = {"lat": 36.7509, "lon": 5.0567}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    for name in ("CORE_URL", "NOMINATIM_URL", "OSRM_URL"):
        monkeypatch.delenv(name, raising=False)
    from app import core_client, db, indicators, logistics, planning
    from app.config import parse_config

    def activate(tenant="A", **config):
        db.upsert_tenant(tenant, "key-" + tenant, parse_config({"depot": DEPOT, **config}))
        logistics.ingest(tenant, "stock_level", "S", 1, {"sku": "SKU", "quantite_disponible": 1000})

    def order(ref, lat=36.76, lon=5.06, tenant="A", **extra):
        logistics.ingest(tenant, "commande", ref, 1, {
            "statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 1}], "latitude": lat, "longitude": lon, **extra,
        })

    def set_event_time(shipment_id, status, iso, tenant="A"):
        with db.connect() as c:
            c.execute("UPDATE shipment_events SET at = ? WHERE tenant_id = ? AND shipment_id = ? AND status = ?",
                      (iso, tenant, shipment_id, status))

    activate()
    return db, logistics, indicators, planning, core_client, activate, order, set_event_time, monkeypatch


def plan_with_eta(db, shipment_ids, eta="2026-11-08T08:30:00", tenant="A"):
    """Store a plan whose stops have a chosen planned arrival (local time)."""
    stops = [
        {"vehicle_id": "V1", "seq": i, "shipment_id": sid, "order_ref": sid.removeprefix("EXP-"), "address": None,
         "lat": 36.76, "lon": 5.06, "demand": 1, "eta": eta, "leg_distance_m": 1000, "leg_duration_s": 100}
        for i, sid in enumerate(shipment_ids, start=1)
    ]
    summary = {"source": "straight_line", "stops_planned": len(stops), "vehicles_used": 1, "total_distance_m": 10_000,
               "baseline_distance_m": 20_000, "gain_pct": 50.0,
               "routes": [{"vehicle_id": "V1", "stops": len(stops), "load": len(stops), "capacity": 100,
                           "distance_m": 10_000, "duration_s": 3600, "back_at_depot": "2026-11-08T12:00:00"}]}
    return db.save_plan(tenant, "2026-11-08", summary, stops)


# --------------------------------------------------------------------------- indicators
def test_indicators_of_a_company_without_activity(env):
    _, _, indicators, *_ = env
    figures = indicators.compute("A")
    assert figures["shipments"]["total"] == 0 and figures["deliveries"]["on_time_rate_pct"] is None
    assert figures["routes"]["gain_pct"] is None and figures["routes"]["km_per_route"] is None


def test_on_time_rate_compares_delivery_with_the_planned_arrival_in_utc(env):
    db, logistics, indicators, _, _, _, order, set_event_time, _ = env
    for ref in ("ON", "LATE"):
        order(ref)
    plan_with_eta(db, ["EXP-ON", "EXP-LATE"], eta="2026-11-08T08:30:00")  # 07:30 UTC with the default +60 offset
    for sid, delivered in (("EXP-ON", "2026-11-08T07:44:00+00:00"), ("EXP-LATE", "2026-11-08T07:46:00+00:00")):
        logistics.change_status("A", sid, "en_transit")
        logistics.change_status("A", sid, "livree")
        set_event_time(sid, "livree", delivered)
    d = indicators.compute("A")["deliveries"]
    assert (d["delivered"], d["measured_for_punctuality"], d["on_time"], d["on_time_rate_pct"]) == (2, 2, 1, 50.0)


def test_the_company_time_zone_changes_the_verdict(env):
    db, logistics, indicators, _, _, activate, order, set_event_time, _ = env
    activate(utc_offset_minutes=0)
    order("X")
    plan_with_eta(db, ["EXP-X"], eta="2026-11-08T08:30:00")  # offset 0: due 08:30 UTC
    logistics.change_status("A", "EXP-X", "en_transit")
    logistics.change_status("A", "EXP-X", "livree")
    set_event_time("EXP-X", "livree", "2026-11-08T08:40:00+00:00")
    assert indicators.compute("A")["deliveries"]["on_time"] == 1


def test_deliveries_that_were_never_planned_are_not_counted_for_punctuality(env):
    db, logistics, indicators, _, _, _, order, _, _ = env
    order("NOPLAN")
    logistics.change_status("A", "EXP-NOPLAN", "en_transit")
    logistics.change_status("A", "EXP-NOPLAN", "livree")
    d = indicators.compute("A")["deliveries"]
    assert d["delivered"] == 1 and d["measured_for_punctuality"] == 0 and d["on_time_rate_pct"] is None


def test_route_figures_come_from_the_stored_plans(env):
    db, logistics, indicators, planning, _, _, order, _, _ = env
    for i in range(4):
        order(f"R{i}", 36.75 + 0.01 * i, 5.05 + 0.01 * i)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    r = indicators.compute("A")["routes"]
    assert r["plans"] == 1 and r["latest_plan"] == plan["plan_id"] and r["routes_planned"] == plan["vehicles_used"]
    assert r["total_km"] == pytest.approx(plan["total_distance_m"] / 1000, abs=0.1)
    assert r["gain_pct"] == pytest.approx(plan["gain_pct"], abs=0.1)
    assert r["km_per_route"] == pytest.approx(plan["total_distance_m"] / 1000 / plan["vehicles_used"], abs=0.1)
    assert r["distance_source"] == "straight_line"


def test_failed_deliveries_are_counted_but_cancellations_are_not(env):
    _, logistics, indicators, _, _, _, order, _, _ = env
    order("F")
    order("C")
    logistics.change_status("A", "EXP-F", "en_transit")
    logistics.fail_delivery("A", "EXP-F", "Customer absent")
    logistics.change_status("A", "EXP-C", "retour", "Cancelled before pick-up")
    d = indicators.compute("A")["deliveries"]
    assert d["failed"] == 1


# --------------------------------------------------------------------------- alerts
def test_a_shipment_late_on_its_planned_arrival_raises_one_delay_alert(env):
    db, logistics, indicators, _, _, _, order, _, _ = env
    order("D")
    plan_with_eta(db, ["EXP-D"], eta="2026-11-08T08:30:00")  # due 07:30 UTC
    soon = datetime(2026, 11, 8, 7, 40, tzinfo=timezone.utc)
    assert indicators.check_delays("A", soon) == 0 and db.list_alerts("A") == []  # inside the 15 minute tolerance
    late = datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc)
    assert indicators.check_delays("A", late) == 1
    indicators.check_delays("A", late)
    [alert] = db.list_alerts("A")
    assert alert["kind"] == "delay" and "30 minutes" in alert["message"]
    logistics.change_status("A", "EXP-D", "en_transit")
    logistics.change_status("A", "EXP-D", "livree")
    assert db.list_alerts("A") == []  # delivered: the delay is over


def test_a_failed_delivery_raises_an_alert_with_the_reason_until_it_is_delivered_again(env):
    db, logistics, _, _, _, _, order, _, _ = env
    order("F")
    logistics.change_status("A", "EXP-F", "en_transit")
    logistics.fail_delivery("A", "EXP-F", "Wrong address", reschedule_date="2026-11-09")
    [alert] = db.list_alerts("A")
    assert alert["kind"] == "delivery_failed" and "Wrong address" in alert["message"]
    logistics.change_status("A", "EXP-F-R1", "en_transit")
    logistics.change_status("A", "EXP-F-R1", "livree")
    assert db.list_alerts("A") == []


# --------------------------------------------------------------------------- rescheduling
def test_a_rescheduled_delivery_becomes_a_new_shipment_for_the_later_day(env):
    db, logistics, _, planning, _, _, order, _, _ = env
    order("R")
    logistics.change_status("A", "EXP-R", "en_transit")
    result = logistics.fail_delivery("A", "EXP-R", "Customer absent", "2026-11-09")
    new = result["rescheduled"]
    assert result["shipment"]["status"] == "retour"
    assert (new["shipment_id"], new["status"], new["earliest_date"]) == ("EXP-R-R1", "preparation", "2026-11-09")
    assert new["tracking_number"] != result["shipment"]["tracking_number"] and new["lat"] == 36.76
    assert db.get_shipment_by_order("A", "R")["shipment_id"] == "EXP-R-R1"  # the order now follows the new shipment
    assert planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)["deferred"][0]["shipment_id"] == "EXP-R-R1"
    assert [s["shipment_id"] for s in planning.plan_routes("A", "2026-11-09", solver_seconds=0.5)["stops"]] == ["EXP-R-R1"]
    logistics.change_status("A", "EXP-R-R1", "en_transit")  # and a second failure gives R2
    assert logistics.fail_delivery("A", "EXP-R-R1", "Closed", "2026-11-10")["rescheduled"]["shipment_id"] == "EXP-R-R2"


def test_without_a_date_a_failed_delivery_is_not_rescheduled(env):
    db, logistics, _, _, _, _, order, _, _ = env
    order("N")
    assert logistics.fail_delivery("A", "EXP-N", "Refused")["rescheduled"] is None
    assert len(db.list_shipments("A")) == 1


@pytest.mark.parametrize("reason, date_, code", [("", "2026-11-09", 422), ("Closed", "tomorrow", 422)])
def test_a_bad_failure_report_changes_nothing(env, reason, date_, code):
    db, logistics, *_, order, _, _ = env
    order("B")
    logistics.change_status("A", "EXP-B", "en_transit")
    with pytest.raises(logistics.TransitionError) as caught:
        logistics.fail_delivery("A", "EXP-B", reason, date_)
    assert caught.value.code == code
    assert db.get_shipment("A", "EXP-B")["status"] == "en_transit" and len(db.list_shipments("A")) == 1


def test_cancelling_the_order_cancels_the_rescheduled_shipment(env):
    db, logistics, *_, order, _, _ = env
    order("K")
    logistics.change_status("A", "EXP-K", "en_transit")
    logistics.fail_delivery("A", "EXP-K", "Absent", "2026-11-09")
    logistics.ingest("A", "commande", "K", 2, {"statut": "annulee", "lignes": [{"sku": "SKU", "quantite": 1}]})
    assert db.get_shipment("A", "EXP-K-R1")["status"] == "retour"


def test_operations_api_reschedules_and_exposes_indicators_and_codes(env):
    db, logistics, *_, order, _, _ = env
    from app.main import app

    order("API")
    logistics.change_status("A", "EXP-API", "en_transit")
    http, h = TestClient(app), {"X-Tenant-Id": "A", "Authorization": "Bearer key-A"}
    r = http.post("/tenant/A/shipments/EXP-API/status", headers=h,
                  json={"status": "retour", "reason": "Absent", "reschedule_date": "2026-11-09"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["status"] == "retour" and body["rescheduled_as"]["shipment_id"] == "EXP-API-R1"
    bad = http.post("/tenant/A/shipments/EXP-API-R1/status", headers=h, json={"status": "retour", "reason": "x", "reschedule_date": "no"})
    assert bad.status_code == 422 and bad.json()["status"] == "error"

    figures = http.get("/tenant/A/indicators", headers=h).json()["data"]
    assert figures["shipments"]["total"] == 2 and figures["deliveries"]["failed"] == 1

    codes = http.post("/tenant/A/access-codes", headers=h).json()["data"]
    assert codes["sign_in"] == "/ui/login?tenant=A" and set(codes["codes"]) == {"manager", "driver"}
    other = {"X-Tenant-Id": "A", "Authorization": "Bearer wrong"}
    assert http.post("/tenant/A/access-codes", headers=other).status_code == 401
    assert http.get("/tenant/A/indicators", headers=other).status_code == 401


# --------------------------------------------------------------------------- alerts sent to the core
class FakeCore:
    def __init__(self):
        self.requests = []
        self.fail = False

    def __call__(self, request):
        self.requests.append(request)
        if self.fail:
            return httpx.Response(500, json={})
        body = json.loads(request.read())
        data = {"id": "x", "version": 1} if request.url.path.startswith("/exchange") else {"event_id": "e"}
        return httpx.Response(200, json={"status": "ok", "data": {**data, "echo": body.get("event_type")}, "error": None})

    def events(self):
        return [json.loads(r.read()) for r in self.requests if r.url.path == "/webhooks/agent-events"]


@pytest.fixture()
def core(env):
    *_, core_client, _, _, _, monkeypatch = env
    fake = FakeCore()
    monkeypatch.setenv("CORE_URL", "http://core.test")
    monkeypatch.setattr(core_client, "transport", httpx.MockTransport(fake))
    return fake


def test_open_alerts_are_sent_once_to_the_core_webhook(env, core):
    db, logistics, _, _, core_client, *_ = env
    logistics.ingest("A", "commande", "NOSTOCK", 1, {
        "statut": "confirmee", "lignes": [{"sku": "NOPE", "quantite": 1}], "latitude": 36.7, "longitude": 5.0,
    })
    assert core_client.send_pending_alerts("A") == {"sent": 1, "failed": 0, "skipped": False}
    [event] = core.events()
    assert event == {"event_type": "stock_unknown", "severity": "low",
                     "data": {"order_ref": "NOSTOCK", "message": event["data"]["message"]}}
    assert core.requests[0].headers["Authorization"] == "Bearer key-A" and core.requests[0].headers["X-Tenant-Id"] == "A"
    assert core_client.send_pending_alerts("A")["sent"] == 0  # not sent twice


def test_a_failed_delivery_is_sent_with_high_severity_and_again_if_it_comes_back(env, core):
    db, logistics, _, _, core_client, _, order, *_ = env
    order("F")
    logistics.change_status("A", "EXP-F", "en_transit")
    logistics.fail_delivery("A", "EXP-F", "Absent")
    core_client.send_pending_alerts("A")
    assert [(e["event_type"], e["severity"]) for e in core.events()] == [("delivery_failed", "high")]
    db.resolve_alerts("A", "F")
    db.raise_alert("A", "F", "delivery_failed", "again")  # the same trouble after it was solved: tell the core again
    core_client.send_pending_alerts("A")
    assert len(core.events()) == 2


def test_alerts_not_accepted_by_the_core_are_retried(env, core):
    db, logistics, _, _, core_client, *_ = env
    db.raise_alert("A", "O", "unplanned", "left out")
    core.fail = True
    assert core_client.send_pending_alerts("A") == {"sent": 0, "failed": 1, "skipped": False}
    core.fail = False
    assert core_client.send_pending_alerts("A")["sent"] == 1


def test_after_a_change_the_shipment_is_published_then_the_alert_is_sent(env, core):
    db, logistics, _, _, core_client, _, order, *_ = env
    order("Z")
    logistics.change_status("A", "EXP-Z", "en_transit")
    logistics.fail_delivery("A", "EXP-Z", "Closed")
    core_client.after_change("A")
    assert [r.url.path for r in core.requests] == ["/exchange/agent/expedition", "/webhooks/agent-events"]


# --------------------------------------------------------------------------- old database files
def test_a_database_made_by_an_older_version_is_upgraded_in_place(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY, api_key TEXT NOT NULL, config_json TEXT NOT NULL, activated_at TEXT NOT NULL);
        CREATE TABLE shipments (tenant_id TEXT NOT NULL, shipment_id TEXT NOT NULL, order_ref TEXT NOT NULL, status TEXT NOT NULL,
            carrier TEXT NOT NULL, tracking_number TEXT NOT NULL, address TEXT, lat REAL, lon REAL, failure_reason TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, publish_pending INTEGER NOT NULL DEFAULT 1,
            published_version INTEGER, PRIMARY KEY (tenant_id, shipment_id));
        CREATE TABLE alerts (tenant_id TEXT NOT NULL, order_ref TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
            created_at TEXT NOT NULL, resolved_at TEXT, PRIMARY KEY (tenant_id, order_ref, kind));
        INSERT INTO shipments VALUES ('A', 'EXP-1', '1', 'preparation', 'c', 't', NULL, NULL, NULL, NULL, 'x', 'x', 1, NULL);
        INSERT INTO alerts VALUES ('A', '1', 'unplanned', 'm', 'x', NULL);
    """)
    old.commit()
    old.close()
    monkeypatch.setenv("DATABASE_PATH", str(path))
    from app import db

    shipment = db.get_shipment("A", "EXP-1")
    assert shipment["earliest_date"] is None and shipment["status"] == "preparation"  # the old row survives
    assert db.list_unnotified_alerts("A")[0]["kind"] == "unplanned"
    db.mark_alert_notified("A", "1", "unplanned")
    assert db.list_unnotified_alerts("A") == []
