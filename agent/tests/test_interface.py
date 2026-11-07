"""Web interface: access codes, signed sessions, dashboard, map and driver pages."""
import json
import re

import pytest
from fastapi.testclient import TestClient

DEPOT = {"lat": 36.7509, "lon": 5.0567}
HX = {"HX-Request": "true"}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    for name in ("CORE_URL", "NOMINATIM_URL", "OSRM_URL", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    from app import db, logistics, ui_auth
    from app.config import parse_config
    from app.main import app

    for tenant in ("A", "B"):
        db.upsert_tenant(tenant, "key-" + tenant, parse_config({"depot": DEPOT, "vehicles": [{"id": "V1", "capacity": 50}]}))
        logistics.ingest(tenant, "stock_level", "S", 1, {"sku": "SKU", "quantite_disponible": 1000})
    ui_auth._failures.clear()
    monkeypatch.setattr(ui_auth, "PBKDF2_ROUNDS", 1_000)  # fast hashing for the tests only

    def order(ref, lat, lon, tenant="A", **extra):
        logistics.ingest(tenant, "commande", ref, 1, {
            "statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 2}], "latitude": lat, "longitude": lon, **extra,
        })

    client = TestClient(app, follow_redirects=False)
    return client, db, logistics, ui_auth, order


def sign_in(world, tenant="A", role="manager"):
    client, _, _, ui_auth, _ = world
    codes = ui_auth.generate_codes(tenant)
    r = client.post("/ui/login", data={"tenant": tenant, "code": codes[role]})
    assert r.status_code == 303
    return codes


# --------------------------------------------------------------------------- codes and sessions
def test_codes_are_readable_distinct_and_only_their_hashes_are_stored(world):
    _, db, _, ui_auth, _ = world
    codes = ui_auth.generate_codes("A")
    assert set(codes) == {"manager", "driver"} and codes["manager"] != codes["driver"]
    for code in codes.values():
        assert re.fullmatch(r"[2-9A-HJ-NP-Z]{4}(-[2-9A-HJ-NP-Z]{4}){2}", code)
    stored = json.dumps(db.list_access_codes("A"))
    assert all(code.replace("-", "") not in stored.replace("-", "") for code in codes.values())


def test_a_code_opens_only_its_own_company_and_role(world):
    _, _, _, ui_auth, _ = world
    codes = ui_auth.generate_codes("A")
    ui_auth.generate_codes("B")
    assert ui_auth.check_code("A", codes["manager"]) == "manager"
    assert ui_auth.check_code("A", codes["driver"]) == "driver"
    assert ui_auth.check_code("A", codes["manager"].lower().replace("-", " ")) == "manager"  # tolerant typing
    assert ui_auth.check_code("B", codes["manager"]) is None
    assert ui_auth.check_code("A", "WRONG-CODE-0000") is None
    assert ui_auth.check_code("NOBODY", codes["manager"]) is None


def test_generating_again_replaces_the_old_codes(world):
    _, _, _, ui_auth, _ = world
    first = ui_auth.generate_codes("A")
    second = ui_auth.generate_codes("A")
    assert ui_auth.check_code("A", first["manager"]) is None
    assert ui_auth.check_code("A", second["manager"]) == "manager"


def test_session_cookies_cannot_be_forged_or_reused_after_expiry_or_deactivation(world):
    _, db, _, ui_auth, _ = world
    token = ui_auth.make_session("A", "manager")
    assert ui_auth.read_session(token)["role"] == "manager"
    body, signature = token.rsplit(".", 1)
    assert ui_auth.read_session(body + "." + "0" * len(signature)) is None
    forged_body = body[:-2] + ("AA" if not body.endswith("AA") else "BB")
    assert ui_auth.read_session(forged_body + "." + signature) is None
    assert ui_auth.read_session(None) is None and ui_auth.read_session("garbage") is None
    other = ui_auth.make_session("A", "manager").replace(".", "x.", 1)
    assert ui_auth.read_session(other) is None
    ui_auth.SESSION_SECONDS, saved = -10, ui_auth.SESSION_SECONDS
    try:
        assert ui_auth.read_session(ui_auth.make_session("A", "manager")) is None  # already expired
    finally:
        ui_auth.SESSION_SECONDS = saved
    db.purge_tenant("A")
    assert ui_auth.read_session(token) is None  # the company is gone


def test_a_session_signed_with_another_secret_is_refused(world, monkeypatch):
    _, _, _, ui_auth, _ = world
    token = ui_auth.make_session("A", "manager")
    monkeypatch.setenv("SESSION_SECRET", "another-secret")
    assert ui_auth.read_session(token) is None


# --------------------------------------------------------------------------- sign-in page
def test_pages_need_a_session(world):
    client = world[0]
    for path in ("/ui", "/ui/driver", "/ui/map", "/ui/fragments/shipments"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/ui/login"
    r = client.get("/ui/fragments/alerts", headers=HX)  # an HTMX fragment cannot follow a redirect
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/ui/login"


def test_sign_in_with_the_manager_code_opens_the_dashboard(world):
    client = world[0]
    codes = world[3].generate_codes("A")
    r = client.post("/ui/login", data={"tenant": "A", "code": codes["manager"]})
    assert r.status_code == 303 and r.headers["location"] == "/ui"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/ui" in cookie
    page = client.get("/ui")
    assert page.status_code == 200 and "Dashboard" in page.text and "hx-get" in page.text


def test_the_driver_code_goes_to_the_driver_page_and_not_to_the_dashboard(world):
    client = world[0]
    codes = world[3].generate_codes("A")
    r = client.post("/ui/login", data={"tenant": "A", "code": codes["driver"]})
    assert r.headers["location"] == "/ui/driver"
    assert client.get("/ui/driver").status_code == 200
    assert client.get("/ui/map").status_code == 200
    assert client.get("/ui").status_code == 403
    assert client.get("/ui/fragments/indicators").status_code == 403
    assert client.post("/ui/plan", headers=HX, data={}).status_code == 403


def test_a_wrong_code_is_refused_and_too_many_attempts_are_blocked(world):
    client, _, _, ui_auth, _ = world
    ui_auth.generate_codes("A")
    for _ in range(5):
        r = client.post("/ui/login", data={"tenant": "A", "code": "AAAA-AAAA-AAAA"})
        assert r.status_code == 401 and "wrong code" in r.text
    blocked = client.post("/ui/login", data={"tenant": "A", "code": ui_auth.generate_codes("A")["manager"]})
    assert blocked.status_code == 429  # even the right code waits


def test_sign_out_removes_the_session(world):
    client = world[0]
    sign_in(world)
    assert client.get("/ui").status_code == 200
    out = client.post("/ui/logout")
    assert out.status_code == 303
    client.cookies.clear()
    assert client.get("/ui").status_code == 303


# --------------------------------------------------------------------------- dashboard
def test_dashboard_fragments_show_this_companys_figures_only(world):
    client, _, _, _, order = world
    order("CMD-A1", 36.76, 5.06)
    order("CMD-A2", 36.77, 5.07)
    order("CMD-B1", 36.78, 5.08, tenant="B")
    sign_in(world)
    shipments = client.get("/ui/fragments/shipments").text
    assert "EXP-CMD-A1" in shipments and "EXP-CMD-A2" in shipments and "CMD-B1" not in shipments
    assert "2" in client.get("/ui/fragments/indicators").text
    assert "No open alert" in client.get("/ui/fragments/alerts").text


def test_actions_must_come_from_the_interface(world):
    client, _, _, _, order = world
    order("CMD-1", 36.76, 5.06)
    sign_in(world)
    assert client.post("/ui/plan", data={}).status_code == 403  # no HX-Request header: a forged form
    assert client.post("/ui/shipments/EXP-CMD-1/status", data={"status": "en_transit"}).status_code == 403
    assert client.post("/ui/shipments/EXP-CMD-1/fail", data={"reason": "x"}).status_code == 403


def test_planning_from_the_dashboard_shows_the_result_and_refreshes_the_page(world):
    client, db, _, _, order = world
    for i in range(4):
        order(f"CMD-{i}", 36.75 + i * 0.01, 5.05 + i * 0.01)
    sign_in(world)
    r = client.post("/ui/plan", headers=HX, data={"plan_date": "2026-11-08"})
    assert r.status_code == 200 and "Plan P1" in r.text and "4 stop(s)" in r.text
    assert r.headers["hx-trigger"] == "refresh"
    assert db.latest_plan("A")["stops_planned"] == 4
    assert "1 plan(s)" in client.get("/ui/fragments/indicators").text
    assert "V1" in client.get("/ui/fragments/shipments").text


def test_planning_without_a_depot_explains_why(world):
    client, db, _, _, _ = world
    from app.config import parse_config

    db.upsert_tenant("A", "key-A", parse_config({}))
    sign_in(world)
    r = client.post("/ui/plan", headers=HX, data={})
    assert r.status_code == 200 and "No depot configured" in r.text


def test_text_coming_from_orders_is_escaped_everywhere(world):
    client, _, _, _, order = world
    evil = "<script>alert('x')</script>"
    order("CMD-X", 36.76, 5.06, adresse_livraison=evil)
    sign_in(world)
    client.post("/ui/plan", headers=HX, data={"plan_date": "2026-11-08"})
    for path in ("/ui/fragments/shipments", "/ui/fragments/driver", "/ui/map"):
        text = client.get(path).text
        assert evil not in text, path
    assert "&lt;script&gt;" in client.get("/ui/fragments/shipments").text


# --------------------------------------------------------------------------- map
def test_map_page_carries_the_plan_for_leaflet(world):
    client, _, _, _, order = world
    for i in range(3):
        order(f"CMD-{i}", 36.75 + i * 0.01, 5.05)
    sign_in(world)
    assert "No plan yet" in client.get("/ui/map").text
    client.post("/ui/plan", headers=HX, data={"plan_date": "2026-11-08"})
    page = client.get("/ui/map").text
    data = json.loads(re.search(r'<script id="plan-data" type="application/json">(.*?)</script>', page, re.S).group(1))
    assert data["plan_id"] == "P1" and data["depot"] == DEPOT and len(data["stops"]) == 3
    assert "leaflet" in page and "tile.openstreetmap.org" in page
    assert client.get("/ui/map?plan=P1").status_code == 200
    assert "No plan yet" in client.get("/ui/map?plan=P99").text


# --------------------------------------------------------------------------- driver page
def planned(world, n=3):
    client, _, _, _, order = world
    for i in range(n):
        order(f"CMD-{i}", 36.75 + i * 0.01, 5.05 + i * 0.01)
    sign_in(world)
    client.post("/ui/plan", headers=HX, data={"plan_date": "2026-11-08"})


def test_driver_sees_the_stops_in_visiting_order(world):
    client = world[0]
    planned(world)
    text = client.get("/ui/fragments/driver").text
    positions = [text.index(f"EXP-CMD-{i}") for i in range(3)]
    stops = {s["shipment_id"]: s["seq"] for s in world[1].latest_plan("A")["stops"]}
    assert [stops[f"EXP-CMD-{i}"] for i in sorted(range(3), key=positions.__getitem__)] == [1, 2, 3]
    assert "Picked up" in text and "Delivered" not in text


def test_driver_follows_a_delivery_from_pick_up_to_delivered(world):
    client, db, _, _, _ = world
    planned(world, 1)
    r = client.post("/ui/shipments/EXP-CMD-0/status", headers=HX, data={"status": "en_transit"})
    assert r.status_code == 200 and "Delivered" in r.text and "Picked up" not in r.text
    assert db.get_shipment("A", "EXP-CMD-0")["status"] == "en_transit"
    r = client.post("/ui/shipments/EXP-CMD-0/status", headers=HX, data={"status": "livree"})
    assert "Nothing to deliver" in r.text and "EXP-CMD-0: delivered" in r.text
    assert [e["status"] for e in db.list_events("A", "EXP-CMD-0")] == ["preparation", "en_transit", "livree"]


def test_driver_cannot_make_an_invalid_move_or_skip_the_failure_form(world):
    client, db, _, _, _ = world
    planned(world, 1)
    assert "Cannot go from" in client.post("/ui/shipments/EXP-CMD-0/status", headers=HX, data={"status": "livree"}).text
    assert "failure form" in client.post("/ui/shipments/EXP-CMD-0/status", headers=HX, data={"status": "retour"}).text
    assert "Shipment not found" in client.post("/ui/shipments/EXP-NOPE/status", headers=HX, data={"status": "en_transit"}).text
    assert db.get_shipment("A", "EXP-CMD-0")["status"] == "preparation"


def test_a_failed_delivery_needs_a_reason_and_can_be_rescheduled(world):
    client, db, _, _, _ = world
    planned(world, 1)
    client.post("/ui/shipments/EXP-CMD-0/status", headers=HX, data={"status": "en_transit"})
    assert "reason is required" in client.post("/ui/shipments/EXP-CMD-0/fail", headers=HX, data={"reason": " "}).text
    r = client.post("/ui/shipments/EXP-CMD-0/fail", headers=HX,
                    data={"reason": "Customer absent", "reschedule_date": "2026-11-09"})
    assert "rescheduled as EXP-CMD-0-R1 for 2026-11-09" in r.text
    assert db.get_shipment("A", "EXP-CMD-0")["failure_reason"] == "Customer absent"
    assert db.get_shipment("A", "EXP-CMD-0-R1")["earliest_date"] == "2026-11-09"
    assert "not before 2026-11-09" in r.text
    assert "bad date" not in client.post("/ui/shipments/EXP-CMD-0-R1/status", headers=HX, data={"status": "x"}).text


def test_a_driver_never_touches_another_companys_shipments(world):
    client, db, _, _, order = world
    order("CMD-B", 36.8, 5.1, tenant="B")
    planned(world, 1)
    r = client.post("/ui/shipments/EXP-CMD-B/status", headers=HX, data={"status": "en_transit"})
    assert "Shipment not found" in r.text
    assert db.get_shipment("B", "EXP-CMD-B")["status"] == "preparation"


def test_a_deactivated_company_loses_its_sessions(world):
    client, db, _, _, _ = world
    sign_in(world)
    assert client.get("/ui").status_code == 200
    db.purge_tenant("A")
    assert client.get("/ui").status_code == 303
    assert db.list_access_codes("A") == []


def test_signing_in_is_not_possible_for_an_unknown_company(world):
    client = world[0]
    r = client.post("/ui/login", data={"tenant": "GHOST", "code": "AAAA-AAAA-AAAA"})
    assert r.status_code == 401 and "Unknown company or wrong code" in r.text
