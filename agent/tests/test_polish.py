"""Version 1.1.0: location realism, polite geocoding under load, shipment pages, filters, static files, sessions."""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

DEPOT = {"lat": 36.7509, "lon": 5.0567}
HX = {"HX-Request": "true"}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    for name in ("CORE_URL", "NOMINATIM_URL", "OSRM_URL", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    from app import db, geo, logistics, ui_auth
    from app.config import parse_config
    from app.main import app

    monkeypatch.setattr(ui_auth, "PBKDF2_ROUNDS", 1_000)
    monkeypatch.setattr(geo, "min_interval", 0)
    ui_auth._failures.clear()
    for tenant in ("A", "B"):
        db.upsert_tenant(tenant, "key-" + tenant, parse_config({"depot": DEPOT, "vehicles": [{"id": "V1", "capacity": 50}]}))
        logistics.ingest(tenant, "stock_level", "S", 1, {"sku": "SKU", "quantite_disponible": 1000})

    def order(ref, lat=None, lon=None, tenant="A", **extra):
        payload = {"statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 2}], **extra}
        if lat is not None:
            payload.update(latitude=lat, longitude=lon)
        logistics.ingest(tenant, "commande", ref, 1, payload)

    client = TestClient(app, follow_redirects=False)
    return client, db, logistics, ui_auth, order, monkeypatch


def sign_in(world, tenant="A", role="manager"):
    client, _, _, ui_auth, *_ = world
    codes = ui_auth.generate_codes(tenant)
    assert client.post("/ui/login", data={"tenant": tenant, "code": codes[role]}).status_code == 303
    return codes


# --------------------------------------------------------------------------- location realism
def test_straight_line_distances_include_the_usual_detour_of_roads(world):
    from app import routing
    from app.geo import haversine_m

    matrix = routing.straight_line_matrix([(36.75, 5.05), (36.80, 5.10)])
    straight = haversine_m(36.75, 5.05, 36.80, 5.10)
    assert matrix.distance_m[0][1] == pytest.approx(straight * routing.STRAIGHT_LINE_DETOUR, abs=1)
    assert matrix.duration_s[0][1] == pytest.approx(matrix.distance_m[0][1] / (routing.STRAIGHT_LINE_SPEED_KMH / 3.6), abs=1)
    assert matrix.snap_m is None


def osrm_with_snaps(snaps):
    def handler(request):
        n = len(snaps)
        return httpx.Response(200, json={
            "code": "Ok", "durations": [[0 if i == j else 60 for j in range(n)] for i in range(n)],
            "distances": [[0 if i == j else 900 for j in range(n)] for i in range(n)],
            "sources": [{"distance": d, "location": [5.0, 36.0]} for d in snaps],
        })
    return handler


def test_osrm_tells_how_far_each_point_is_from_a_road(world):
    from app import routing

    *_, monkeypatch = world
    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(osrm_with_snaps([3.0, 12.5, 2400.0])))
    matrix = routing.build_matrix([(36.75, 5.05), (36.76, 5.06), (36.9, 5.2)])
    assert matrix.source == "osrm" and matrix.snap_m == [3.0, 12.5, 2400.0]


def test_a_stop_far_from_any_road_is_flagged_planned_and_alerted(world):
    client, db, logistics, _, order, monkeypatch = world
    from app import planning, routing

    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(osrm_with_snaps([0, 5, 2400, 8])))
    order("ON-LAND-1", 36.76, 5.06)
    order("IN-THE-SEA", 36.80, 5.12)
    order("ON-LAND-2", 36.77, 5.07)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["stops_planned"] == 3  # still planned: OSRM attaches it to the nearest road
    assert plan["warnings"] == [{"shipment_id": "EXP-IN-THE-SEA", "order_ref": "IN-THE-SEA", "snap_distance_m": 2400}]
    [alert] = [a for a in db.list_alerts("A") if a["kind"] == "location_suspect"]
    assert alert["order_ref"] == "IN-THE-SEA" and "2400 m from the nearest road" in alert["message"]
    again = planning.plan_routes("A", "2026-11-09", solver_seconds=0.5)  # a new plan keeps the alert open
    assert again["warnings"] and [a["kind"] for a in db.list_alerts("A")] == ["location_suspect"]


def test_correcting_the_location_closes_the_suspicious_location_alert(world):
    client, db, _, _, order, monkeypatch = world
    from app import planning, routing

    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(osrm_with_snaps([0, 3000])))
    order("SEA", 36.80, 5.12)
    planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert db.list_alerts("A")
    sign_in(world)
    r = client.post("/ui/shipments/EXP-SEA/location", headers=HX, data={"lat": "36.7515", "lon": "5.0830"})
    assert r.status_code == 200 and r.headers["hx-refresh"] == "true"
    assert db.list_alerts("A") == [] and db.get_shipment("A", "EXP-SEA")["lat"] == 36.7515


def test_no_warning_without_the_road_network_or_when_points_are_on_roads(world):
    client, db, _, _, order, monkeypatch = world
    from app import planning, routing

    order("X", 36.76, 5.06)
    assert planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)["warnings"] == []  # straight lines: cannot tell
    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(osrm_with_snaps([0, 40])))
    assert planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)["warnings"] == []


def test_left_out_stops_are_explained_in_words(world):
    client, db, _, _, order, _ = world
    from app import planning

    order("HEAVY", 36.76, 5.06, lignes=[{"sku": "SKU", "quantite": 500}])
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["unassigned"][0]["explanation"] == "its load is bigger than every vehicle"
    [alert] = db.list_alerts("A")
    assert alert["message"] == "Not planned: its load is bigger than every vehicle."


# --------------------------------------------------------------------------- polite geocoding under load
def test_a_burst_asks_the_geocoder_once_per_address_not_once_per_thread(world):
    client, db, logistics, _, order, monkeypatch = world
    from app import geo

    calls = []
    lock = threading.Lock()

    def handler(request):
        with lock:
            calls.append(request.url.params["q"])
        return httpx.Response(200, json=[{"lat": "36.75", "lon": "5.06"}])

    monkeypatch.setenv("NOMINATIM_URL", "https://nominatim.test")
    monkeypatch.setattr(geo, "transport", httpx.MockTransport(handler))
    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(lambda _: geo.geocode("A", "Gare routiere Bejaia"), range(10)))
    assert all(r.located for r in results) and calls == ["Gare routiere Bejaia"]


def test_many_orders_arriving_together_cause_one_lookup_per_distinct_address(world):
    client, db, logistics, _, order, monkeypatch = world
    from app import geo, planning

    calls = []
    lock = threading.Lock()

    def handler(request):
        with lock:
            calls.append(request.url.params["q"])
        return httpx.Response(200, json=[{"lat": "36.75", "lon": "5.06"}])

    monkeypatch.setenv("NOMINATIM_URL", "https://nominatim.test")
    monkeypatch.setattr(geo, "transport", httpx.MockTransport(handler))
    addresses = ["Place A", "Place B", "Place C", "Place A", "Place B", "Place C", "Place D", "Place A"]
    for i, address in enumerate(addresses):
        order(f"O{i}", adresse_livraison=address)
    with ThreadPoolExecutor(8) as pool:  # what eight simultaneous notifications trigger in the background
        list(pool.map(lambda _: planning.locate_shipments("A"), range(8)))
    assert sorted(calls) == ["Place A", "Place B", "Place C", "Place D"]
    assert all(s["lat"] is not None for s in db.list_shipments("A"))


# --------------------------------------------------------------------------- pages and static files
def test_stylesheet_icon_and_the_favicon_shortcut_are_served(world):
    client, *_ = world
    css = client.get("/ui/static/app.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"] and ".topbar" in css.text
    icon = client.get("/ui/static/favicon.svg")
    assert icon.status_code == 200 and "svg" in icon.headers["content-type"]
    r = client.get("/favicon.ico")
    assert r.status_code == 301 and r.headers["location"] == "/ui/static/favicon.svg"
    assert client.get("/ui/static/nope.css").status_code == 404


def test_pages_ask_for_a_fresh_stylesheet_after_each_release(world):
    client, *_ = world
    from app.version import VERSION

    html = client.get("/ui/login").text
    assert re.search(rf"/ui/static/app\.css\?v={re.escape(VERSION)}-\d+", html) and "<title>Sign in" in html


def test_the_sign_in_page_explains_the_company_identifier(world):
    client, *_ = world
    page = client.get("/ui/login?tenant=abc-123").text
    assert "Company identifier" in page and 'value="abc-123"' in page and "not its name" in page
    wrong = client.post("/ui/login", data={"tenant": "yassir", "code": "AAAA-AAAA-AAAA"})
    assert wrong.status_code == 401 and "not by its name" in wrong.text


def test_errors_inside_the_interface_are_readable_pages_not_json(world):
    client, db, _, _, order, _ = world
    order("MINE", 36.76, 5.06)
    sign_in(world)
    missing = client.get("/ui/shipments/EXP-NOPE")
    assert missing.status_code == 404 and "text/html" in missing.headers["content-type"]
    assert "Not found" in missing.text and "Shipment not found" in missing.text and not missing.text.startswith("{")
    codes = world[3].generate_codes("A")
    driver = TestClient(client.app, follow_redirects=False)
    driver.post("/ui/login", data={"tenant": "A", "code": codes["driver"]})
    denied = driver.get("/ui")
    assert denied.status_code == 403 and "Not allowed" in denied.text and "text/html" in denied.headers["content-type"]
    fragment = driver.get("/ui/fragments/indicators", headers=HX)
    assert fragment.status_code == 403 and fragment.text.startswith('<div class="error"')
    assert client.get("/health").json()["status"] == "ok"  # the contract routes still speak JSON


def test_a_company_cannot_open_the_shipment_page_of_another(world):
    client, db, _, _, order, _ = world
    order("SECRET-B", 36.76, 5.06, tenant="B")
    sign_in(world, "A")
    assert client.get("/ui/shipments/EXP-SECRET-B").status_code == 404
    assert client.get("/ui/fragments/shipments/EXP-SECRET-B").status_code == 404
    assert client.post("/ui/shipments/EXP-SECRET-B/location", headers=HX, data={"lat": "1", "lon": "1"}).text.count("not found") == 1
    assert db.get_shipment("B", "EXP-SECRET-B")["lat"] == 36.76


def test_the_shipment_page_shows_details_history_and_actions(world):
    client, db, logistics, _, order, _ = world
    order("DET", 36.76, 5.06, client={"nom": "Karim"}, adresse_livraison="12 rue Test")
    sign_in(world)
    page = client.get("/ui/shipments/EXP-DET").text
    assert "EXP-DET" in page and "Karim" in page and "12 rue Test" in page and "To prepare" in page
    assert "Picked up, out for delivery" in page and "Set the location by hand" in page
    r = client.post("/ui/shipments/EXP-DET/status", headers=HX, data={"status": "en_transit", "view": "detail"})
    assert "On the road" in r.text and "Delivered" in r.text and "Delivery failed" in r.text
    assert "Set the location by hand" not in r.text  # only while the shipment is still to be prepared
    assert "EXP-DET: on the road" in r.text
    r = client.post("/ui/shipments/EXP-DET/fail", headers=HX, data={"view": "detail", "reason": "Absent", "reschedule_date": "2099-01-02"})
    assert "rescheduled as EXP-DET-R1" in r.text and "Absent" in r.text
    assert "EXP-DET-R1" in client.get("/ui/shipments/EXP-DET").text  # the page links to the other shipment of the order


def test_the_location_form_validates_what_it_gets(world):
    client, db, logistics, _, order, _ = world
    order("LOC", 36.76, 5.06)
    sign_in(world)
    url = "/ui/shipments/EXP-LOC/location"
    assert client.post(url, data={"lat": "1", "lon": "1"}).status_code == 403  # not from the interface
    for bad in ({"lat": "95", "lon": "5"}, {"lat": "abc", "lon": "5"}, {"lat": "36", "lon": ""}, {"lat": "36", "lon": "200"}):
        r = client.post(url, headers=HX, data=bad)
        assert r.status_code == 200 and "Latitude must be between" in r.text, bad
    assert db.get_shipment("A", "EXP-LOC")["lat"] == 36.76
    assert client.post(url, headers=HX, data={"lat": "36,7515", "lon": "5,083"}).headers["hx-refresh"] == "true"  # decimal comma
    assert db.get_shipment("A", "EXP-LOC")["lon"] == 5.083
    logistics.change_status("A", "EXP-LOC", "en_transit")
    assert "only be changed while" in client.post(url, headers=HX, data={"lat": "36", "lon": "5"}).text


def test_the_shipment_list_can_be_filtered_by_status_and_searched(world):
    client, db, logistics, _, order, _ = world
    order("ALPHA", 36.76, 5.06, adresse_livraison="Cite Tobal")
    order("BETA", 36.77, 5.07, adresse_livraison="Gare routiere")
    order("GAMMA", 36.78, 5.08, adresse_livraison="Stade")
    logistics.change_status("A", "EXP-BETA", "en_transit")
    logistics.change_status("A", "EXP-GAMMA", "en_transit")
    logistics.change_status("A", "EXP-GAMMA", "livree")
    sign_in(world)

    def ids(**params):
        text = client.get("/ui/fragments/shipments", params=params).text
        return re.findall(r'href="/ui/shipments/([^"]+)"', text)

    assert ids() == ["EXP-BETA", "EXP-ALPHA", "EXP-GAMMA"]  # on the road first, delivered last
    assert ids(status="en_transit") == ["EXP-BETA"] and ids(status="livree") == ["EXP-GAMMA"] and ids(status="preparation") == ["EXP-ALPHA"]
    assert ids(q="tobal") == ["EXP-ALPHA"] and ids(q="GARE") == ["EXP-BETA"] and ids(q="order") == []
    assert ids(status="nonsense") == ids()  # an unknown status is ignored, not an error
    assert "No shipment matches" in client.get("/ui/fragments/shipments", params={"q": "zzz"}).text
    assert "Search order" not in client.get("/ui/fragments/shipments").text  # the search box is not inside the refreshed part


def test_alerts_come_with_a_title_advice_and_a_link(world):
    client, db, logistics, _, order, _ = world
    order("NOADDR", lignes=[{"sku": "SKU", "quantite": 1}])
    order("NOSTOCK", 36.7, 5.0, lignes=[{"sku": "OTHER", "quantite": 1}])
    sign_in(world)
    text = client.get("/ui/fragments/alerts").text
    assert "Address missing" in text and "What to do" in text and "Add the delivery address" in text
    assert "Stock unknown" in text and "NOSTOCK" in text
    order("OK", 36.76, 5.06)
    db.raise_alert("A", "OK", "delay", "late")
    assert 'href="/ui/shipments/EXP-OK"' in client.get("/ui/fragments/alerts").text


def test_the_latest_plan_card_shows_each_vehicle_and_how_full_it_is(world):
    client, db, logistics, _, order, _ = world
    for i in range(3):
        order(f"P{i}", 36.75 + i * 0.01, 5.05)
    sign_in(world)
    assert "No plan yet" in client.get("/ui/fragments/plan").text
    client.post("/ui/plan", headers=HX, data={"plan_date": "2026-11-08"})
    text = client.get("/ui/fragments/plan").text
    assert "V1" in text and "Load 6 / 50 units" in text and "estimated from straight lines" in text


def test_the_map_page_offers_every_plan_and_marks_stops_far_from_roads(world):
    client, db, logistics, _, order, monkeypatch = world
    from app import planning, routing

    order("M1", 36.76, 5.06)
    planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(osrm_with_snaps([0, 4000])))
    planning.plan_routes("A", "2026-11-09", solver_seconds=0.5)
    sign_in(world)
    page = client.get("/ui/map").text
    assert "P1 - 2026-11-08" in page and "P2 - 2026-11-09" in page and 'id="plan"' in page
    data = json.loads(re.search(r'<script id="plan-data" type="application/json">(.*?)</script>', page, re.S).group(1))
    assert data["plan_id"] == "P2" and data["warnings"][0]["snap_distance_m"] == 4000
    assert "red ring" in page and "road network" in page
    older = json.loads(re.search(r'<script id="plan-data" type="application/json">(.*?)</script>',
                                 client.get("/ui/map?plan=P1").text, re.S).group(1))
    assert older["plan_id"] == "P1" and older["warnings"] == []


def test_the_driver_page_counts_what_is_left_and_links_to_details_and_a_map(world):
    client, db, logistics, _, order, _ = world
    order("D1", 36.76, 5.06)
    order("D2", 36.77, 5.07)
    logistics.change_status("A", "EXP-D1", "en_transit")
    sign_in(world, role="driver")
    text = client.get("/ui/fragments/driver").text
    assert "2 stop(s) left, 1 on the road" in text
    assert 'href="/ui/shipments/EXP-D1"' in text and "openstreetmap.org/?mlat=36.76" in text
    assert "On the road" in text and "To prepare" in text


# --------------------------------------------------------------------------- sessions survive a restart
def test_the_session_key_is_kept_in_the_data_volume(world, tmp_path):
    client, db, logistics, ui_auth, order, monkeypatch = world
    token = ui_auth.make_session("A", "manager")
    key_file = tmp_path / "session.key"
    assert key_file.exists() and len(key_file.read_bytes()) == 32
    monkeypatch.setattr(ui_auth, "_random_secret", b"a-different-key-after-a-restart-xxx")  # a new process
    assert ui_auth.read_session(token)["role"] == "manager"  # still valid: the key came from the file


def test_a_configured_secret_wins_and_a_read_only_volume_does_not_break_sign_in(world, monkeypatch):
    client, db, logistics, ui_auth, order, _ = world
    monkeypatch.setenv("SESSION_SECRET", "from-the-environment")
    assert ui_auth._secret() == b"from-the-environment"
    monkeypatch.delenv("SESSION_SECRET")

    def refuse(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_bytes", refuse)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    token = ui_auth.make_session("A", "driver")
    assert ui_auth.read_session(token)["role"] == "driver"


def test_one_version_number_everywhere(world):
    from app.main import VERSION as served
    from app.main import app
    from app.version import VERSION

    manifest = json.loads((Path(__file__).resolve().parents[1] / "agent.manifest.json").read_text(encoding="utf-8"))
    assert VERSION == served == app.version == manifest["version"]
