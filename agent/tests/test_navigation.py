"""Version 1.2.0: navigation links for the phone, and real road lines on the map."""
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

DEPOT = {"lat": 36.7509, "lon": 5.0567}
HX = {"HX-Request": "true"}
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    for name in ("CORE_URL", "NOMINATIM_URL", "OSRM_URL", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    from app import db, logistics, ui_auth
    from app.config import parse_config
    from app.main import app

    monkeypatch.setattr(ui_auth, "PBKDF2_ROUNDS", 1_000)
    ui_auth._failures.clear()
    for tenant in ("A", "B"):
        db.upsert_tenant(tenant, "key-" + tenant, parse_config({"depot": DEPOT, "vehicles": [{"id": "V1", "capacity": 3}, {"id": "V2", "capacity": 3}]}))
        logistics.ingest(tenant, "stock_level", "S", 1, {"sku": "SKU", "quantite_disponible": 1000})

    def order(ref, lat=None, lon=None, tenant="A", qty=1, **extra):
        payload = {"statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": qty}], **extra}
        if lat is not None:
            payload.update(latitude=lat, longitude=lon)
        logistics.ingest(tenant, "commande", ref, 1, payload)

    return TestClient(app, follow_redirects=False), db, ui_auth, order, monkeypatch


def sign_in(world, tenant="A", role="manager"):
    client, _, ui_auth, *_ = world
    codes = ui_auth.generate_codes(tenant)
    assert client.post("/ui/login", data={"tenant": tenant, "code": codes[role]}).status_code == 303


def google_target(href: str):
    """(destination, waypoints) of a Google Maps directions link."""
    query = parse_qs(urlparse(href).query)
    waypoints = unquote(query["waypoints"][0]).split("|") if "waypoints" in query else []
    return query["destination"][0], waypoints


# --------------------------------------------------------------------------- navigation links
def test_the_navigation_links_open_directions_to_the_point():
    from app import ui_routes

    href = ui_routes.nav_url(36.7524, 5.0854)
    assert href.startswith("https://www.google.com/maps/dir/?api=1") and "travelmode=driving" in href
    assert google_target(href) == ("36.752400,5.085400", [])
    assert "daddr=36.752400,5.085400" in ui_routes.apple_url(36.7524, 5.0854)


def test_a_whole_route_link_lists_the_stops_in_order_and_stops_at_ten():
    from app import ui_routes

    pts = [(36.70 + i / 100, 5.00 + i / 100) for i in range(12)]
    assert ui_routes.route_nav_url([]) is None
    assert google_target(ui_routes.route_nav_url(pts[:1])) == ("36.700000,5.000000", [])
    destination, stops = google_target(ui_routes.route_nav_url(pts))
    assert destination == "36.790000,5.090000"  # the 10th stop: Google Maps takes ten at a time
    assert stops == [f"{lat:.6f},{lon:.6f}" for lat, lon in pts[:9]]
    assert "%7C" in ui_routes.route_nav_url(pts[:3])  # the separator is URL-encoded


def test_the_driver_page_has_a_navigate_button_per_stop_and_one_for_the_whole_route(world):
    client, db, ui_auth, order, _ = world
    for i in range(3):
        order(f"N{i}", 36.75 + i * 0.01, 5.05, qty=1)
    order("NOPOINT", adresse_livraison="somewhere")  # a stop without coordinates has nothing to navigate to
    sign_in(world, role="driver")
    from app import planning

    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    van = plan["routes"][0]["vehicle_id"]  # the solver may use either van
    text = client.get(f"/ui/fragments/driver?vehicle={van}").text
    buttons = re.findall(r'class="button ghost big"[^>]*href="(https://www\.google\.com/maps/dir/[^"]+)"', text)
    assert len(buttons) == 3 and all("destination=36.7" in b for b in buttons)
    assert "https://maps.apple.com/?daddr=" in text
    route = re.search(r'class="button primary big"[^>]*href="([^"]+)"', text)
    assert route and "Navigate the next 3 stop(s)" in text
    destination, waypoints = google_target(route.group(1).replace("&amp;", "&"))
    assert len(waypoints) == 2 and "sends the stops' coordinates to Google Maps" in text


def test_a_stop_far_from_any_road_is_flagged_and_left_out_of_the_route_link(world):
    client, db, ui_auth, order, monkeypatch = world
    from app import planning, routing

    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(osrm(snaps=[0, 5, 3000, 8])))
    order("OK1", 36.75, 5.05)
    order("SEA", 36.80, 5.12)
    order("OK2", 36.76, 5.06)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["warnings"][0]["order_ref"] == "SEA"
    sign_in(world, role="driver")
    text = client.get("/ui/fragments/driver").text
    assert text.count("Check the location") == 1 and 'href="/ui/shipments/EXP-SEA"' in text
    route = re.search(r'class="button primary big"[^>]*href="([^"]+)"', text).group(1).replace("&amp;", "&")
    destination, waypoints = google_target(route)
    assert "36.800000,5.120000" not in [destination, *waypoints] and "Navigate the next 2 stop(s)" in text
    assert text.count("Navigate</a>") == 3  # each stop still has its own button: the warning is the driver's cue


def test_the_whole_route_link_needs_one_vehicle(world):
    client, db, ui_auth, order, _ = world
    from app import planning

    for i in range(4):  # two vans of capacity 3: the stops are split between them
        order(f"M{i}", 36.75 + i * 0.05, 5.05 + i * 0.05, qty=2)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["vehicles_used"] == 2
    sign_in(world, role="driver")
    both = client.get("/ui/fragments/driver").text
    assert "Navigate the next" not in both and "Choose one vehicle" in both
    one = client.get("/ui/fragments/driver?vehicle=" + plan["routes"][0]["vehicle_id"]).text
    assert "Navigate the next" in one


def test_the_shipment_page_and_the_map_offer_navigation(world):
    client, db, ui_auth, order, _ = world
    from app import planning

    order("S1", 36.76, 5.06)
    order("NOCOORD", adresse_livraison="somewhere")
    planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    sign_in(world)
    page = client.get("/ui/shipments/EXP-S1").text
    assert "destination=36.760000,5.060000" in page
    assert "destination=" not in client.get("/ui/shipments/EXP-NOCOORD").text
    assert '"https://www.google.com/maps/dir/?api=1&destination="' in client.get("/ui/map").text  # built in the popup


# --------------------------------------------------------------------------- road lines
def osrm(snaps=None, line=None, route_status=200, record=None):
    def handler(request):
        if record is not None:
            record.append(request)
        path = request.url.path
        if path.startswith("/table/"):
            n = path.count(";") + 1
            return httpx.Response(200, json={
                "code": "Ok", "durations": [[0 if i == j else 120 for j in range(n)] for i in range(n)],
                "distances": [[0 if i == j else 1500 for j in range(n)] for i in range(n)],
                "sources": [{"distance": (snaps or [0] * n)[i]} for i in range(n)],
            })
        if route_status != 200:
            return httpx.Response(route_status, json={})
        return httpx.Response(200, json={"code": "Ok", "routes": [{"geometry": {"type": "LineString", "coordinates": line or [[5.05, 36.75], [5.06, 36.76], [5.07, 36.77]]}}]})
    return handler


def test_the_road_line_comes_back_as_lat_lon(world):
    *_, monkeypatch = world
    from app import routing

    seen = []
    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(osrm(record=seen)))
    line = routing.route_geometry([(36.75, 5.05), (36.77, 5.07)])
    assert line == [[36.75, 5.05], [36.76, 5.06], [36.77, 5.07]]  # OSRM answers lon,lat: flipped
    request = seen[0]
    assert request.url.path == "/route/v1/driving/5.050000,36.750000;5.070000,36.770000"
    assert request.url.params["overview"] == "full" and request.url.params["geometries"] == "geojson"


@pytest.mark.parametrize("case", ["no_url", "one_point", "http500", "down", "bad_code", "bad_shape"])
def test_a_missing_road_network_never_breaks_anything(world, case):
    *_, monkeypatch = world
    from app import routing

    if case != "no_url":
        monkeypatch.setenv("OSRM_URL", "http://osrm.test")

    def handler(request):
        if case == "down":
            raise httpx.ConnectError("refused")
        if case == "http500":
            return httpx.Response(500, json={})
        if case == "bad_code":
            return httpx.Response(200, json={"code": "NoRoute"})
        return httpx.Response(200, json={"code": "Ok", "routes": []})

    monkeypatch.setattr(routing, "transport", httpx.MockTransport(handler))
    points = [(36.75, 5.05)] if case == "one_point" else [(36.75, 5.05), (36.77, 5.07)]
    assert routing.route_geometry(points) is None


def test_a_plan_made_with_the_road_network_keeps_the_road_line_of_each_vehicle(world):
    client, db, ui_auth, order, monkeypatch = world
    from app import planning, routing

    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(osrm()))
    for i in range(2):
        order(f"R{i}", 36.75 + i * 0.01, 5.05)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["road_geometry"] is True and plan["source"] == "osrm"
    paths = db.get_plan_paths("A", plan["plan_id"])
    assert list(paths) == [plan["routes"][0]["vehicle_id"]] and len(paths[plan["routes"][0]["vehicle_id"]]) == 3

    sign_in(world)
    page = client.get("/ui/map").text
    data = json.loads(re.search(r'<script id="plan-data" type="application/json">(.*?)</script>', page, re.S).group(1))
    assert data["paths"] == paths and "real roads" in page
    api = client.get(f"/tenant/A/plans/{plan['plan_id']}/paths", headers={"X-Tenant-Id": "A", "Authorization": "Bearer key-A"})
    assert api.status_code == 200 and api.json()["data"] == paths


def test_without_the_road_network_the_plan_has_no_road_line_and_says_so(world):
    client, db, ui_auth, order, _ = world
    from app import planning

    order("X", 36.76, 5.06)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["road_geometry"] is False and db.get_plan_paths("A", plan["plan_id"]) == {}
    sign_in(world)
    assert "not the roads" in client.get("/ui/map").text


def test_a_failing_road_line_does_not_stop_the_plan(world):
    client, db, ui_auth, order, monkeypatch = world
    from app import planning, routing

    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(osrm(route_status=500)))
    order("Y", 36.76, 5.06)
    plan = planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    assert plan["stops_planned"] == 1 and plan["road_geometry"] is False and plan["source"] == "osrm"


def test_road_lines_belong_to_one_company_and_go_with_it(world):
    client, db, ui_auth, order, monkeypatch = world
    from app import planning, routing

    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(osrm()))
    order("A1", 36.76, 5.06)
    order("B1", 36.77, 5.07, tenant="B")
    planning.plan_routes("A", "2026-11-08", solver_seconds=0.5)
    planning.plan_routes("B", "2026-11-08", solver_seconds=0.5)
    a, b = {"X-Tenant-Id": "A", "Authorization": "Bearer key-A"}, {"X-Tenant-Id": "B", "Authorization": "Bearer key-B"}
    assert client.get("/tenant/A/plans/P1/paths", headers=b).status_code == 403
    assert client.get("/tenant/A/plans/P99/paths", headers=a).status_code == 404
    assert client.get("/tenant/A/plans/P1/paths", headers={**a, "Authorization": "Bearer wrong"}).status_code == 401
    assert db.get_plan_paths("A", "P1") and db.get_plan_paths("B", "P1")
    client.post("/tenant/deactivate", headers=a)
    assert db.get_plan_paths("A", "P1") == {} and db.get_plan_paths("B", "P1")


def test_the_map_gets_a_view_before_any_line_is_drawn():
    """Leaflet throws when a line is added to a map without a view: the route lines silently vanished in 1.1.0."""
    script = (Path(__file__).resolve().parents[1] / "app" / "templates" / "map.html").read_text(encoding="utf-8")
    first_view = script.index("map.setView(")
    assert first_view < script.index("L.layerGroup().addTo(map)") and first_view < script.index("L.polyline(")


def test_the_routing_service_can_answer_big_plans():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "--max-table-size 1000" in compose  # OSRM's default limit is 100 points: a bigger plan would fall back to straight lines
