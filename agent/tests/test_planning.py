"""Route planning for a company: locating shipments, building and storing plans, and the plan API."""
import httpx
import pytest
from fastapi.testclient import TestClient

DEPOT = {"lat": 36.7509, "lon": 5.0567}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    for name in ("CORE_URL", "NOMINATIM_URL", "OSRM_URL", "NOMINATIM_COUNTRY"):
        monkeypatch.delenv(name, raising=False)
    from app import db, geo, logistics, planning
    from app.config import parse_config

    monkeypatch.setattr(geo, "min_interval", 0)

    def activate(tenant="A", **config):
        db.upsert_tenant(tenant, "key-" + tenant, parse_config(config))
        logistics.ingest(tenant, "stock_level", "S", 1, {"sku": "SKU", "quantite_disponible": 10_000})

    def order(ref, lat=None, lon=None, qty=1, tenant="A", **extra):
        payload = {"statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": qty}], **extra}
        if lat is not None:
            payload.update(latitude=lat, longitude=lon)
        logistics.ingest(tenant, "commande", ref, 1, payload)

    activate(depot=DEPOT, vehicles=[{"id": "V1", "capacity": 100}])
    return db, planning, activate, order, monkeypatch


def spread(order, n, tenant="A", qty=1):
    for i in range(n):
        order(f"CMD-{i}", 36.75 + 0.01 * (i % 4), 5.05 + 0.01 * (i // 4), qty=qty, tenant=tenant)


# --------------------------------------------------------------------------- planning
def test_a_plan_needs_a_depot_and_an_activated_company(env):
    db, planning, activate, _, _ = env
    activate("NODEPOT")
    with pytest.raises(planning.PlanError) as no_depot:
        planning.plan_routes("NODEPOT")
    assert no_depot.value.code == 422 and "depot" in str(no_depot.value)
    with pytest.raises(planning.PlanError) as unknown:
        planning.plan_routes("GHOST")
    assert unknown.value.code == 404


def test_a_plan_visits_every_located_shipment_once_and_is_stored(env):
    db, planning, _, order, _ = env
    spread(order, 6)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["plan_id"] == "P1" and plan["plan_date"] == "2026-11-08"
    assert plan["stops_planned"] == 6 and plan["vehicles_used"] == 1 and plan["unassigned"] == []
    assert sorted(s["shipment_id"] for s in plan["stops"]) == [f"EXP-CMD-{i}" for i in range(6)]
    assert [s["seq"] for s in plan["stops"]] == [1, 2, 3, 4, 5, 6]
    assert plan["source"] == "straight_line"
    assert plan["total_distance_m"] > 0 and plan["baseline_distance_m"] >= plan["total_distance_m"]
    assert db.get_plan("A", "P1") == plan
    assert db.latest_plan("A")["plan_id"] == "P1"


def test_arrival_times_start_at_the_beginning_of_the_working_day(env):
    _, planning, activate, order, _ = env
    activate(depot=DEPOT, vehicles=[{"id": "V1", "capacity": 100}], working_hours={"start": "07:30", "end": "18:00"},
             stop_duration_minutes=20)
    spread(order, 3)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    etas = [s["eta"] for s in plan["stops"]]
    assert etas[0] > "2026-11-08T07:30:00" and etas == sorted(etas)
    assert all(e.startswith("2026-11-08T") for e in etas)
    gap = (int(etas[1][11:13]) * 60 + int(etas[1][14:16])) - (int(etas[0][11:13]) * 60 + int(etas[0][14:16]))
    assert gap >= 20  # at least the stop duration between two stops
    route = plan["routes"][0]
    assert route["back_at_depot"] > etas[-1] and route["stops"] == 3


def test_demand_is_the_sum_of_the_order_lines(env):
    db, planning, _, order, _ = env
    order("CMD-BIG", 36.76, 5.06, qty=7.2)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["stops"][0]["demand"] == 8 and plan["routes"][0]["load"] == 8


def test_a_second_plan_replaces_the_first_as_latest_and_skips_shipments_on_the_road(env):
    db, planning, _, order, _ = env
    from app import logistics

    spread(order, 3)
    planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    logistics.change_status("A", "EXP-CMD-0", "en_transit")
    second = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert second["plan_id"] == "P2" and second["stops_planned"] == 2
    assert "EXP-CMD-0" not in [s["shipment_id"] for s in second["stops"]]
    assert [p["plan_id"] for p in db.list_plans("A")] == ["P2", "P1"]


def test_vehicles_share_the_load_when_one_is_not_enough(env):
    _, planning, activate, order, _ = env
    activate(depot=DEPOT, vehicles=[{"id": "small-1", "capacity": 4}, {"id": "small-2", "capacity": 4}])
    spread(order, 6, qty=1)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["vehicles_used"] == 2 and plan["stops_planned"] == 6
    assert all(r["load"] <= r["capacity"] for r in plan["routes"])


def test_stops_that_cannot_be_served_are_listed_with_the_reason_and_an_alert(env):
    db, planning, activate, order, _ = env
    activate(depot=DEPOT, vehicles=[{"id": "V1", "capacity": 5}], working_hours={"start": "08:00", "end": "09:00"})
    order("CMD-OK", 36.755, 5.06, qty=1)
    order("CMD-HEAVY", 36.756, 5.061, qty=50)
    order("CMD-FAR", 38.0, 7.0, qty=1)  # about 280 km away: impossible within one hour
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    reasons = {u["order_ref"]: u["reason"] for u in plan["unassigned"]}
    assert reasons == {"CMD-HEAVY": "exceeds_capacity", "CMD-FAR": "too_far_for_working_hours"}
    assert [s["order_ref"] for s in plan["stops"]] == ["CMD-OK"]
    assert {a["order_ref"] for a in db.list_alerts("A") if a["kind"] == "unplanned"} == {"CMD-HEAVY", "CMD-FAR"}


def test_an_order_wanted_later_is_deferred_until_its_day(env):
    _, planning, _, order, _ = env
    order("CMD-NOW", 36.755, 5.06)
    order("CMD-LATER", 36.756, 5.061, date_livraison_souhaitee="2026-11-10T00:00:00Z")
    today = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert [s["order_ref"] for s in today["stops"]] == ["CMD-NOW"]
    assert today["deferred"] == [{"shipment_id": "EXP-CMD-LATER", "order_ref": "CMD-LATER", "requested_date": "2026-11-10"}]
    assert "CMD-LATER" in [s["order_ref"] for s in planning.plan_routes("A", "2026-11-10", solver_seconds=0.5)["stops"]]


@pytest.mark.parametrize("kwargs, text", [
    ({"plan_date": "tomorrow"}, "ISO date"),
    ({"plan_date": "2026-13-45"}, "ISO date"),
    ({"solver_seconds": 0}, "solver_seconds"),
    ({"solver_seconds": 500}, "solver_seconds"),
])
def test_bad_parameters_are_refused(env, kwargs, text):
    _, planning, _, order, _ = env
    order("CMD-1", 36.755, 5.06)
    with pytest.raises(planning.PlanError) as caught:
        planning.plan_routes("A", **kwargs)
    assert caught.value.code == 422 and text in str(caught.value)


def test_no_shipment_means_no_stored_plan(env):
    db, planning, _, _, _ = env
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["plan_id"] is None and plan["stops"] == []
    assert db.list_plans("A") == []


def test_one_company_never_plans_another_companys_shipments(env):
    db, planning, activate, order, _ = env
    activate("B", depot=DEPOT)
    order("CMD-A", 36.755, 5.06, tenant="A")
    order("CMD-B", 36.756, 5.061, tenant="B")
    plan_b = planning.plan_routes("B", "2026-11-08", solver_seconds=0.5)
    assert [s["order_ref"] for s in plan_b["stops"]] == ["CMD-B"]
    planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    db.purge_tenant("A")
    assert db.list_plans("A") == [] and db.get_plan("A", "P1") is None
    assert db.get_plan("B", "P1") is not None


# --------------------------------------------------------------------------- locating text addresses
def fake_nominatim(env, answer):
    db, _, _, _, monkeypatch = env
    from app import geo

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=answer(request) if callable(answer) else answer)

    monkeypatch.setenv("NOMINATIM_URL", "https://nominatim.test")
    monkeypatch.setattr(geo, "transport", httpx.MockTransport(handler))
    return calls


def test_text_addresses_are_geocoded_then_planned(env):
    db, planning, _, order, _ = env
    calls = fake_nominatim(env, [{"lat": "36.76", "lon": "5.07"}])
    order("CMD-TXT", adresse_livraison="5 rue des Freres, Bejaia")
    assert db.get_shipment("A", "EXP-CMD-TXT")["lat"] is None
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["stops"][0]["lat"] == 36.76 and plan["stops"][0]["lon"] == 5.07
    assert len(calls) == 1
    assert [a for a in db.list_alerts("A")] == []


def test_an_address_nobody_knows_raises_an_alert_and_is_left_out(env):
    db, planning, _, order, _ = env
    fake_nominatim(env, [])
    order("CMD-LOST", adresse_livraison="Nowhere street")
    order("CMD-OK", 36.755, 5.06)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert [s["order_ref"] for s in plan["stops"]] == ["CMD-OK"]
    assert plan["unassigned"] == [{"shipment_id": "EXP-CMD-LOST", "order_ref": "CMD-LOST", "reason": "no_location",
                                   "explanation": "it has no coordinates yet"}]
    [alert] = db.list_alerts("A")
    assert alert["kind"] == "address_not_found" and "Nowhere street" in alert["message"]


def test_without_a_geocoder_the_alert_asks_for_coordinates(env):
    db, planning, _, order, _ = env
    order("CMD-TXT", adresse_livraison="5 rue X")
    planning.locate_shipments("A")
    [alert] = db.list_alerts("A")
    assert alert["kind"] == "address_not_located" and "geocoding is not enabled" in alert["message"]


def test_setting_the_location_by_hand_closes_the_alert(env):
    db, planning, _, order, _ = env
    from app.main import app

    order("CMD-TXT", adresse_livraison="5 rue X")
    planning.locate_shipments("A")
    r = TestClient(app).put(
        "/tenant/A/shipments/EXP-CMD-TXT/location",
        headers={"X-Tenant-Id": "A", "Authorization": "Bearer key-A"}, json={"lat": 36.77, "lon": 5.08},
    )
    assert r.status_code == 200 and r.json()["data"]["lat"] == 36.77
    assert db.list_alerts("A") == []
    assert planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)["stops_planned"] == 1


def test_a_pushed_order_is_located_after_the_reply(env):
    db, _, _, _, _ = env
    from app.main import app

    fake_nominatim(env, [{"lat": "36.8", "lon": "5.1"}])
    http = TestClient(app)
    http.post("/exchange/notify", headers={"X-Tenant-Id": "A", "Authorization": "Bearer key-A"}, json={
        "object_type": "commande", "external_id": "CMD-PUSH", "version": 1,
        "payload": {"statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 1}], "adresse_livraison": "7 rue Y"},
    })
    assert db.get_shipment("A", "EXP-CMD-PUSH")["lat"] == 36.8


# --------------------------------------------------------------------------- API
def test_plan_api_end_to_end(env):
    _, _, _, order, _ = env
    from app.main import app

    spread(order, 4)
    http, h = TestClient(app), {"X-Tenant-Id": "A", "Authorization": "Bearer key-A"}
    assert http.get("/tenant/A/plans/latest", headers=h).status_code == 404
    assert http.get("/tenant/A/plans", headers=h).json()["data"] == []

    r = http.post("/tenant/A/plans", headers=h, json={"plan_date": "2026-11-08", "solver_seconds": 0.5})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    plan = r.json()["data"]
    assert plan["plan_id"] == "P1" and plan["stops_planned"] == 4

    assert http.get("/tenant/A/plans/latest", headers=h).json()["data"]["plan_id"] == "P1"
    assert http.get("/tenant/A/plans/P1", headers=h).json()["data"]["stops"] == plan["stops"]
    assert http.get("/tenant/A/plans/P9", headers=h).status_code == 404
    listed = http.get("/tenant/A/plans", headers=h).json()["data"]
    assert [p["plan_id"] for p in listed] == ["P1"] and "stops" not in listed[0]

    # no body at all is accepted: today's date and the default solver time
    assert http.post("/tenant/A/plans", headers=h, json={"solver_seconds": 0.5}).status_code == 200


@pytest.mark.parametrize("body, code", [
    ({"plan_date": "nope"}, 422), ({"solver_seconds": 99}, 422), ({"solver_seconds": "fast"}, 422),
])
def test_plan_api_refuses_bad_requests_in_the_envelope(env, body, code):
    from app.main import app

    r = TestClient(app).post("/tenant/A/plans", headers={"X-Tenant-Id": "A", "Authorization": "Bearer key-A"}, json=body)
    assert r.status_code == code and r.json()["status"] == "error" and r.json()["data"] is None


def test_plan_and_location_routes_need_the_right_key(env):
    _, _, activate, order, _ = env
    from app.main import app

    activate("B", depot=DEPOT)
    order("CMD-1", 36.755, 5.06)
    http = TestClient(app)
    right, other = {"X-Tenant-Id": "A", "Authorization": "Bearer key-A"}, {"X-Tenant-Id": "B", "Authorization": "Bearer key-B"}
    assert http.post("/tenant/A/plans", json={}).status_code == 400
    assert http.post("/tenant/A/plans", headers={**right, "Authorization": "Bearer nope"}, json={}).status_code == 401
    assert http.post("/tenant/A/plans", headers=other, json={}).status_code == 403
    assert http.get("/tenant/A/plans/latest", headers=other).status_code == 403
    assert http.put("/tenant/A/shipments/EXP-CMD-1/location", headers=other, json={"lat": 1, "lon": 1}).status_code == 403
    assert http.put("/tenant/B/shipments/EXP-CMD-1/location", headers=other, json={"lat": 1, "lon": 1}).status_code == 404


def test_location_can_only_be_set_in_preparation_and_must_be_valid(env):
    _, _, _, order, _ = env
    from app import logistics
    from app.main import app

    order("CMD-1", 36.755, 5.06)
    http, h = TestClient(app), {"X-Tenant-Id": "A", "Authorization": "Bearer key-A"}
    url = "/tenant/A/shipments/EXP-CMD-1/location"
    assert http.put(url, headers=h, json={"lat": 95, "lon": 5}).status_code == 422
    assert http.put(url, headers=h, json={"lat": 36}).status_code == 422
    assert http.put("/tenant/A/shipments/EXP-NOPE/location", headers=h, json={"lat": 36, "lon": 5}).status_code == 404
    logistics.change_status("A", "EXP-CMD-1", "en_transit")
    assert http.put(url, headers=h, json={"lat": 36, "lon": 5}).status_code == 409
