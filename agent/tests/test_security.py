"""Security hardening: request size limits, protective headers, injection-proof pages, integrity of external
scripts, bounded memory, unprivileged container start-up."""
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
HX = {"HX-Request": "true"}
DEPOT = {"lat": 36.7509, "lon": 5.0567}


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
    db.upsert_tenant("A", "key-A", parse_config({"depot": DEPOT, "vehicles": [{"id": "V1", "capacity": 50}]}))
    logistics.ingest("A", "stock_level", "S", 1, {"sku": "SKU", "quantite_disponible": 1000})
    return TestClient(app, follow_redirects=False), db, ui_auth, logistics, monkeypatch


def auth():
    return {"X-Tenant-Id": "A", "Authorization": "Bearer key-A"}


def sign_in(world, role="manager"):
    client, _, ui_auth, *_ = world
    codes = ui_auth.generate_codes("A")
    assert client.post("/ui/login", data={"tenant": "A", "code": codes[role]}).status_code == 303


# --------------------------------------------------------------------------- request size
def test_an_oversized_body_is_refused_before_it_is_read_in_full(world):
    client, *_ = world
    from app.main import MAX_BODY_BYTES

    big = json.dumps({"object_type": "commande", "external_id": "X", "version": 1, "payload": {"pad": "x" * (MAX_BODY_BYTES + 10)}})
    r = client.post("/exchange/notify", headers={**auth(), "Content-Type": "application/json"}, content=big)
    assert r.status_code == 413 and r.json() == {"status": "error", "data": None, "error": "Request body too large"}
    assert client.post("/ui/login", data={"tenant": "A", "code": "x" * (MAX_BODY_BYTES + 10)}).status_code == 413
    assert client.post("/tenant/activate", headers=auth(), content=big).status_code == 413


def test_a_body_without_a_declared_length_is_counted_as_it_arrives(world):
    client, *_ = world
    from app.main import MAX_BODY_BYTES

    def chunks():
        for _ in range(12):
            yield b"a=" + b"x" * (MAX_BODY_BYTES // 10)  # no Content-Length: the sender streams it

    r = client.post("/ui/login", content=chunks(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413 and r.json()["error"] == "Request body too large"


def test_normal_sized_requests_still_work(world):
    client, db, *_ = world
    payload = {"statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 1}] * 500, "latitude": 36.7, "longitude": 5.0}
    assert len(json.dumps(payload)) > 10_000
    r = client.post("/exchange/notify", headers=auth(), json={"object_type": "commande", "external_id": "BIG", "version": 1, "payload": payload})
    assert r.status_code == 200 and r.json()["data"]["processed"] is True


# --------------------------------------------------------------------------- headers
def test_pages_cannot_be_framed_cached_or_leak_their_origin(world):
    client, *_ = world
    for path in ("/ui/login",):
        h = client.get(path).headers
        assert h["x-frame-options"] == "DENY" and h["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "same-origin"
    sign_in(world)
    assert client.get("/ui").headers["cache-control"] == "no-store"
    assert client.get("/ui/fragments/alerts").headers["x-frame-options"] == "DENY"
    css = client.get("/ui/static/app.css").headers
    assert css["x-content-type-options"] == "nosniff" and "no-store" not in css.get("cache-control", "")  # assets stay cacheable


def test_error_pages_carry_the_headers_too(world):
    client, *_ = world
    missing = client.get("/ui/nope")
    assert missing.status_code == 404 and missing.headers["x-frame-options"] == "DENY"


# --------------------------------------------------------------------------- injection
def test_a_value_reaching_a_button_cannot_add_fields_to_it(world):
    """Rendered directly with a hostile vehicle name, so it does not depend on what the configuration allows."""
    from app import ui_routes

    evil = 'x","status":"retour","reason":"hacked\' onmouseover=\'alert(1)'
    items = [{"status": status, "shipment_id": f"EXP-{i}", "order_ref": str(i), "vehicle": evil, "seq": i, "eta": "2026-11-08T08:00:00",
              "lat": 36.7, "lon": 5.0, "address": "a", "earliest_date": None, "suspect": False}
             for i, status in enumerate(("preparation", "en_transit"), 1)]
    text = ui_routes.templates.get_template("_driver_list.html").render(
        items=items, vehicle=evil, error=None, notice=None, on_road=1, route_link=None, route_link_count=0,
        tomorrow="2026-11-09", labels=ui_routes.LABELS)
    values = [html.unescape(v) for v in re.findall(r"hx-vals='(.*?)'", text)]
    assert len(values) == 2, "both buttons carry their values"
    for raw in values:
        parsed = json.loads(raw)  # still valid JSON...
        assert set(parsed) == {"status", "vehicle"} and parsed["vehicle"] == evil  # ...with exactly the two fields, the text kept as text

    class Attributes(HTMLParser):  # every attribute name of every element the browser would build from this HTML
        names: set = set()

        def handle_starttag(self, tag, attrs):
            self.names.update(name for name, _ in attrs)

    parser = Attributes()
    parser.names = set()
    parser.feed(text)
    assert not {n for n in parser.names if n.startswith("on")}  # the text cannot break out of its attribute and add a handler


def test_text_from_orders_and_the_address_bar_is_escaped_in_every_page(world):
    client, db, ui_auth, logistics, _ = world
    evil = "<img src=x onerror=alert(1)>"
    logistics.ingest("A", "commande", "O2", 1, {"statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 1}], "latitude": 36.7, "longitude": 5.0,
                                              "adresse_livraison": evil, "client": {"nom": evil}})
    sign_in(world)
    for path in ("/ui/fragments/driver", "/ui/fragments/shipments", "/ui/shipments/EXP-O2", "/ui/map", "/ui/driver?vehicle=" + evil,
                 "/ui/login?tenant=" + evil, "/ui/fragments/driver?vehicle=" + evil):
        assert evil not in client.get(path).text, path


# --------------------------------------------------------------------------- external scripts
def test_every_script_and_style_from_another_site_is_pinned_to_its_content():
    pattern = re.compile(r"<(?:script|link)\b[^>]*?(?:src|href)=\"(https?://[^\"]+)\"[^>]*>")
    found = 0
    for template in (ROOT / "app" / "templates").glob("*.html"):
        for tag in pattern.finditer(template.read_text(encoding="utf-8")):
            found += 1
            text = tag.group(0)
            assert re.search(r'integrity="sha384-[A-Za-z0-9+/=]{64}"', text), (template.name, text)
            assert 'crossorigin="anonymous"' in text, (template.name, text)
            assert re.search(r"@\d+\.\d+\.\d+", tag.group(1)), ("unpinned version", tag.group(1))
    assert found >= 5  # htmx, and Leaflet (script and style) on the two pages that draw maps


# --------------------------------------------------------------------------- bounded memory and inputs
def test_failed_sign_in_attempts_cannot_grow_memory_without_limit(world):
    client, db, ui_auth, *_ = world
    ui_auth._failures.clear()
    for i in range(ui_auth.MAX_TRACKED_KEYS + 50):
        ui_auth.record_failure(f"company-{i}|1.2.3.4")
    assert len(ui_auth._failures) <= ui_auth.MAX_TRACKED_KEYS


def test_expired_attempts_are_forgotten_first(world):
    *_, ui_auth, _, monkeypatch = world
    ui_auth._failures.clear()
    old = ui_auth.time.monotonic() - 10_000
    for i in range(ui_auth.MAX_TRACKED_KEYS):
        ui_auth._failures[f"old-{i}"] = [old]
    ui_auth.record_failure("fresh|1.2.3.4")
    assert list(ui_auth._failures) == ["fresh|1.2.3.4"]


def test_absurdly_long_sign_in_fields_are_cut_and_refused(world):
    client, db, ui_auth, *_ = world
    ui_auth._failures.clear()
    r = client.post("/ui/login", data={"tenant": "T" * 50_000, "code": "C" * 50_000})
    assert r.status_code == 401
    assert all(len(key) < 200 for key in ui_auth._failures)


def test_a_plan_has_an_upper_bound_so_one_company_cannot_exhaust_the_agent(world):
    client, db, ui_auth, logistics, monkeypatch = world
    from app import planning

    monkeypatch.setattr(planning, "MAX_PLAN_STOPS", 2)
    for i in range(3):
        logistics.ingest("A", "commande", f"P{i}", 1, {"statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 1}], "latitude": 36.7 + i / 100, "longitude": 5.0})
    r = client.post("/tenant/A/plans", headers=auth(), json={"solver_seconds": 0.5})
    assert r.status_code == 422 and "Too many shipments" in r.json()["error"] and r.json()["status"] == "error"
    assert db.list_plans("A") == []


# --------------------------------------------------------------------------- container
def test_the_container_gives_up_root_before_starting_the_server(tmp_path, monkeypatch):
    import entrypoint

    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (data / "agent.db").write_text("x")
    (data / "sub" / "f").write_text("y")
    calls = []
    # raising=False: these functions only exist on Linux and macOS, and the test also runs on Windows
    monkeypatch.setattr(entrypoint.os, "chown", lambda path, uid, gid: calls.append(("chown", Path(path).name, uid, gid)), raising=False)
    monkeypatch.setattr(entrypoint.os, "setgroups", lambda groups: calls.append(("setgroups", tuple(groups))), raising=False)
    monkeypatch.setattr(entrypoint.os, "setgid", lambda gid: calls.append(("setgid", gid)), raising=False)
    monkeypatch.setattr(entrypoint.os, "setuid", lambda uid: calls.append(("setuid", uid)), raising=False)
    monkeypatch.setattr(entrypoint.os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(entrypoint.os, "execvp", lambda program, args: calls.append(("exec", program, tuple(args))))
    monkeypatch.setenv("DATABASE_PATH", str(data / "agent.db"))
    entrypoint.main()
    kinds = [c[0] for c in calls]
    assert kinds.index("chown") < kinds.index("setgroups") < kinds.index("setgid") < kinds.index("setuid") < kinds.index("exec")  # order matters
    assert {c[1] for c in calls if c[0] == "chown"} >= {"data", "sub", "agent.db", "f"}  # the volume and everything in it
    assert all(c[2:] == (10001, 10001) for c in calls if c[0] == "chown") and ("setuid", 10001) in calls
    assert calls[-1] == ("exec", "uvicorn", ("uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"))


def test_a_container_already_started_as_a_normal_user_just_starts_the_server(monkeypatch):
    import entrypoint

    calls = []
    monkeypatch.setattr(entrypoint.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(entrypoint.os, "chown", lambda *a: calls.append("chown"), raising=False)
    monkeypatch.setattr(entrypoint.os, "setuid", lambda *a: calls.append("setuid"), raising=False)
    monkeypatch.setattr(entrypoint.os, "execvp", lambda program, args: calls.append("exec"))
    entrypoint.main()
    assert calls == ["exec"]


def test_the_image_starts_through_the_entrypoint_and_writes_no_bytecode():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "entrypoint.py"]' in dockerfile and "entrypoint.py" in dockerfile.split("COPY app")[1]
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile


def test_the_session_key_file_is_private(world):
    client, db, ui_auth, *_ = world
    import os
    import stat

    ui_auth.make_session("A", "manager")
    key_file = Path(os.environ["DATABASE_PATH"]).parent / "session.key"
    if os.name == "posix":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert key_file.exists()
