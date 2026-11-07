"""Final verification: the checklist of the instructor's developer guide, as automated tests.

  1. The mandatory endpoints answer with the standard envelope  -> TestEnvelope
  2. The manifest is valid and consistent with the code        -> TestManifest
  3. Two test companies never see each other                   -> TestIsolation
  4. /tenant/deactivate really deletes the data               -> TestPurge
  5. /exchange/notify works with a test object                 -> test_contract.py, test_logistics.py
  6. Speed and concurrency                                     -> TestPerformance
"""
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DEPOT = {"lat": 36.7509, "lon": 5.0567}
ROOT = Path(__file__).resolve().parents[2]


def headers(tenant, key=None):
    return {"X-Tenant-Id": tenant, "Authorization": f"Bearer {key or 'key-' + tenant}"}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "final.db"))
    for name in ("CORE_URL", "NOMINATIM_URL", "OSRM_URL", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    from app import db, ui_auth
    from app.main import app

    monkeypatch.setattr(ui_auth, "PBKDF2_ROUNDS", 1_000)
    return TestClient(app, raise_server_exceptions=False), db, tmp_path / "final.db"


def tenant_tables(path) -> list[str]:
    """Every table of the database file that has a tenant_id column, found by looking at the file itself."""
    with sqlite3.connect(path) as c:
        return [
            t for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if "tenant_id" in [row[1] for row in c.execute(f"PRAGMA table_info({t})")]
        ]


def is_envelope(body) -> bool:
    return (
        isinstance(body, dict) and set(body) == {"status", "data", "error"} and body["status"] in ("ok", "error")
        and (body["error"] is None if body["status"] == "ok" else body["data"] is None and isinstance(body["error"], str))
    )


def contract_routes():
    """Every route the core or an operator can call, taken from the application's public OpenAPI description
    (so it does not depend on FastAPI's internals). The browser pages are left out of it on purpose."""
    from app.main import app

    for path, operations in sorted(app.openapi()["paths"].items()):
        if path.startswith(("/ui", "/docs", "/redoc", "/openapi.json", "/favicon.ico")):
            continue
        for method in sorted(operations):
            if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                yield method.upper(), re.sub(r"\{[^}]+\}", "x", path)


# --------------------------------------------------------------------------- 1. envelope
class TestEnvelope:
    def test_there_are_contract_routes_to_check(self):
        routes = list(contract_routes())
        paths = {p for _, p in routes}
        assert {"/health", "/tenant/activate", "/tenant/deactivate", "/config-schema", "/exchange/notify"} <= paths
        assert "/tenant/x/status" in paths and len(routes) >= 19

    @pytest.mark.parametrize("auth", ["none", "wrong_key", "unknown_tenant"])
    def test_every_route_answers_in_the_envelope_even_when_refused(self, world, auth):
        client, db, _ = world
        from app.config import parse_config

        db.upsert_tenant("x", "right-key", parse_config({}))
        sent = {"none": {}, "wrong_key": headers("x", "wrong"), "unknown_tenant": headers("ghost")}[auth]
        for method, path in contract_routes():
            body = {} if method in ("POST", "PUT") else None
            r = client.request(method, path, headers=sent, json=body)
            assert is_envelope(r.json()), (method, path, r.status_code, r.text[:200])

    def test_unknown_routes_and_wrong_methods_keep_the_envelope(self, world):
        client, *_ = world
        for method, path in (("GET", "/nope"), ("POST", "/health"), ("DELETE", "/tenant/activate"), ("GET", "/tenant/a/b/c/d")):
            r = client.request(method, path)
            assert r.status_code in (404, 405) and is_envelope(r.json()), (method, path)

    @pytest.mark.parametrize("raw, content_type", [
        (b"{not json", "application/json"), (b"[]", "application/json"), (b'"text"', "application/json"),
        (b"", "application/json"), (b"a=1", "application/x-www-form-urlencoded"), (b"\xff\xfe", "application/json"),
    ])
    def test_malformed_bodies_are_refused_in_the_envelope(self, world, raw, content_type):
        client, db, _ = world
        from app.config import parse_config

        db.upsert_tenant("A", "key-A", parse_config({}))
        for path in ("/tenant/activate", "/exchange/notify"):
            r = client.post(path, content=raw, headers={**headers("A"), "Content-Type": content_type})
            assert r.status_code == 422 and is_envelope(r.json()), (path, raw)

    def test_an_internal_failure_is_an_envelope_and_does_not_leak_details(self, world, monkeypatch):
        client, db, _ = world
        from app import main

        def boom(*_):
            raise RuntimeError("secret internal detail /srv/path")

        monkeypatch.setattr(main.db, "get_tenant", boom)
        r = client.get("/tenant/A/status")
        assert r.status_code == 500 and is_envelope(r.json())
        assert "secret internal detail" not in r.text and "RuntimeError" not in r.text

    def test_health_has_the_uptime_the_contract_asks_for(self, world):
        client, *_ = world
        body = client.get("/health").json()
        assert is_envelope(body) and isinstance(body["data"]["uptime_seconds"], int)

    def test_the_five_answers_have_exactly_the_data_of_the_contract(self, world):
        client, *_ = world
        a = client.post("/tenant/activate", json={"config": {}}, headers=headers("A"))
        assert a.json()["data"] == {"activated": True}
        assert client.get("/tenant/A/status").json()["data"] == {"state": "running"}
        schema = client.get("/config-schema").json()["data"]
        assert schema["type"] == "object" and "properties" in schema
        d = client.post("/tenant/deactivate", headers=headers("A"))
        assert d.json()["data"] == {"deactivated": True, "data_purged": True}


# --------------------------------------------------------------------------- 2. manifest
class TestManifest:
    manifest = json.loads((ROOT / "agent" / "agent.manifest.json").read_text(encoding="utf-8"))

    def test_fields_of_the_contract_example_are_all_present(self):
        m = self.manifest
        for key in ("slug", "name", "version", "description", "filiere", "category", "developer", "team_name",
                    "pricing_model", "price", "endpoints", "produces", "consumes"):
            assert key in m, key
        assert m["filiere"] == "automatisation_metier"
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", m["slug"])
        assert re.fullmatch(r"\d+\.\d+\.\d+", m["version"])
        assert "@" in m["developer"]["email"] and "TODO" not in m["developer"]["email"].upper()
        assert {"base_url", "health", "activate", "deactivate", "status", "config_schema", "exchange_notify"} <= set(m["endpoints"])

    def test_the_manifest_matches_the_code(self):
        from app.main import CONSUMES, VERSION, app

        assert self.manifest["version"] == VERSION == app.version
        assert tuple(self.manifest["consumes"]) == CONSUMES
        assert self.manifest["produces"] == ["expedition"]
        paths = set(app.openapi()["paths"])
        for name in ("health", "activate", "deactivate", "status", "config_schema", "exchange_notify"):
            assert self.manifest["endpoints"][name] in paths, name

    def test_the_declared_port_is_the_one_the_container_listens_on(self):
        dockerfile = (ROOT / "agent" / "Dockerfile").read_text(encoding="utf-8")
        port = re.search(r":(\d+)$", self.manifest["endpoints"]["base_url"]).group(1)
        entrypoint = (ROOT / "agent" / "entrypoint.py").read_text(encoding="utf-8")
        assert f"EXPOSE {port}" in dockerfile and f'PORT = "{port}"' in entrypoint and "entrypoint.py" in dockerfile

    def test_object_types_follow_the_data_contract_registry(self):
        assert set(self.manifest["consumes"]) == {"commande", "stock_level"} and self.manifest["produces"] == ["expedition"]

    def test_the_dockerfile_needs_no_manual_configuration(self):
        dockerfile = (ROOT / "agent" / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY app ./app" in dockerfile and "ENV DATABASE_PATH" in dockerfile and "mkdir -p /data" in dockerfile
        assert "HEALTHCHECK" in dockerfile

    def test_every_dependency_is_pinned(self):
        for name in ("requirements.txt", "requirements-dev.txt"):
            for line in (ROOT / "agent" / name).read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("-r"):
                    assert "==" in line, (name, line)

    def test_no_secret_is_written_in_the_code(self):
        pattern = re.compile(r"""(api[_-]?key|secret|password|token)\s*=\s*["'][^"']{6,}["']""", re.I)
        offenders = [str(p) for p in (ROOT / "agent" / "app").rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
        assert offenders == []


# --------------------------------------------------------------------------- 3. isolation
def full_scenario(client, tenant, tag):
    """Everything a company can have: config, stock, orders, a plan, a delivery, a failure, access codes."""
    client.post("/tenant/activate", headers=headers(tenant), json={"config": {"depot": DEPOT, "vehicles": [{"id": "V" + tag, "capacity": 50}]}})
    client.post("/exchange/notify", headers=headers(tenant), json={
        "object_type": "stock_level", "external_id": "S" + tag, "version": 1,
        "payload": {"sku": "SKU" + tag, "quantite_disponible": 100},
    })
    for i in range(3):
        client.post("/exchange/notify", headers=headers(tenant), json={
            "object_type": "commande", "external_id": f"ORD{tag}{i}", "version": 1,
            "payload": {"statut": "confirmee", "lignes": [{"sku": "SKU" + tag, "quantite": 1}],
                        "adresse_livraison": f"{tag} rue {i}", "latitude": 36.75 + i * 0.01, "longitude": 5.05},
        })
    client.post("/exchange/notify", headers=headers(tenant), json={  # an order that raises an alert
        "object_type": "commande", "external_id": f"BAD{tag}", "version": 1,
        "payload": {"statut": "confirmee", "lignes": [{"sku": "NOPE" + tag, "quantite": 1}], "latitude": 36.7, "longitude": 5.0},
    })
    client.post(f"/tenant/{tenant}/plans", headers=headers(tenant), json={"plan_date": "2026-11-08", "solver_seconds": 0.5})
    client.post(f"/tenant/{tenant}/shipments/EXP-ORD{tag}0/status", headers=headers(tenant), json={"status": "en_transit"})
    client.post(f"/tenant/{tenant}/shipments/EXP-ORD{tag}0/status", headers=headers(tenant),
                json={"status": "retour", "reason": "Absent " + tag, "reschedule_date": "2026-11-09"})
    client.post(f"/tenant/{tenant}/access-codes", headers=headers(tenant))


class TestIsolation:
    def test_two_companies_never_see_each_other_through_any_route(self, world):
        client, db, _ = world
        full_scenario(client, "A", "AAA")
        full_scenario(client, "B", "BBB")
        for tenant, other in (("A", "BBB"), ("B", "AAA")):
            for path in ("shipments", "orders", "alerts", "plans", "plans/latest", "indicators"):
                r = client.get(f"/tenant/{tenant}/{path}?include_resolved=true", headers=headers(tenant))
                assert r.status_code == 200, (tenant, path)
                assert other not in r.text, (tenant, path)
            assert client.get(f"/tenant/{tenant}/shipments", headers=headers(tenant)).json()["data"], "scenario produced nothing"

    def test_one_companys_key_opens_nothing_of_another(self, world):
        client, *_ = world
        full_scenario(client, "A", "AAA")
        full_scenario(client, "B", "BBB")
        for path in ("shipments", "orders", "alerts", "plans", "indicators"):
            assert client.get(f"/tenant/B/{path}", headers=headers("B", "key-A")).status_code == 401
            assert client.get(f"/tenant/B/{path}", headers=headers("A")).status_code == 403
        assert client.post("/exchange/notify", headers=headers("B", "key-A"), json={
            "object_type": "commande", "external_id": "X", "version": 1, "payload": {}}).status_code == 401
        assert client.post("/tenant/deactivate", headers=headers("B", "key-A")).status_code == 401

    def test_every_table_with_company_data_is_filtered_by_tenant(self, world):
        client, db, path = world
        full_scenario(client, "A", "AAA")
        full_scenario(client, "B", "BBB")
        with sqlite3.connect(path) as c:
            tables = [t for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                columns = [row[1] for row in c.execute(f"PRAGMA table_info({table})")]
                if "tenant_id" not in columns:
                    continue
                values = {row[0] for row in c.execute(f"SELECT DISTINCT tenant_id FROM {table}")}
                assert values <= {"A", "B"}, table
                for row in c.execute(f"SELECT * FROM {table} WHERE tenant_id = 'A'"):
                    assert "BBB" not in json.dumps(row, default=str) or table == "tenants", (table, row)

    def test_the_interface_shows_one_company_only(self, world):
        client, db, _ = world
        from app import ui_auth

        full_scenario(client, "A", "AAA")
        full_scenario(client, "B", "BBB")
        codes = ui_auth.generate_codes("A")
        web = TestClient(client.app, follow_redirects=False)
        web.post("/ui/login", data={"tenant": "A", "code": codes["manager"]})
        for page in ("/ui/fragments/shipments", "/ui/fragments/alerts", "/ui/fragments/driver", "/ui/map"):
            assert "BBB" not in web.get(page).text, page
        assert "ORDAAA" in web.get("/ui/fragments/shipments").text or "AAA" in web.get("/ui/fragments/shipments").text
        assert web.post("/ui/login", data={"tenant": "B", "code": codes["manager"]}).status_code == 401  # A's code opens only A


# --------------------------------------------------------------------------- 4. purge
class TestPurge:
    def test_the_purge_list_covers_every_table_that_holds_company_data(self, world):
        client, db, path = world
        client.post("/tenant/activate", headers=headers("A"), json={"config": {}})  # creates the file and all tables
        with sqlite3.connect(path) as c:
            with_tenant = {
                t for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if "tenant_id" in [row[1] for row in c.execute(f"PRAGMA table_info({t})")]
            }
        assert with_tenant == set(db.TENANT_TABLES), with_tenant ^ set(db.TENANT_TABLES)

    def test_deactivation_leaves_not_one_row_of_that_company_and_all_of_the_other(self, world):
        client, db, path = world
        from app import ui_auth

        full_scenario(client, "A", "AAA")
        full_scenario(client, "B", "BBB")
        ui_auth.generate_codes("A")
        db.geocache_put("A", "some address", 36.7, 5.0, True)
        db.save_plan("A", "2026-11-09", {"routes": []}, [], {"road-van": [[36.7, 5.0], [36.8, 5.1]]})  # a road line (needs OSRM in real life)
        tables = tenant_tables(path)  # read from the file, not from the purge list under test
        assert len(tables) >= 9
        with sqlite3.connect(path) as c:
            before_b = {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE tenant_id='B'").fetchone()[0] for t in tables}
            assert all(c.execute(f"SELECT COUNT(*) FROM {t} WHERE tenant_id='A'").fetchone()[0] > 0 for t in tables),                 "the scenario must put data in every table"
        r = client.post("/tenant/deactivate", headers=headers("A"))
        assert r.json()["data"] == {"deactivated": True, "data_purged": True}
        with sqlite3.connect(path) as c:
            for table in tables:
                assert c.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id='A'").fetchone()[0] == 0, table
                assert c.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id='B'").fetchone()[0] == before_b[table], table
        assert client.get("/tenant/A/status").status_code == 404

    def test_a_company_that_comes_back_starts_from_nothing(self, world):
        client, db, _ = world
        full_scenario(client, "A", "AAA")
        client.post("/tenant/deactivate", headers=headers("A"))
        client.post("/tenant/activate", headers=headers("A", "new-key"), json={"config": {}})
        assert db.list_shipments("A") == [] and db.list_alerts("A") == [] and db.list_objects("A", "commande") == []
        assert client.get("/tenant/A/shipments", headers=headers("A", "key-A")).status_code == 401  # the old key is gone

    def test_deactivating_twice_or_an_unknown_company_is_harmless(self, world):
        client, *_ = world
        assert client.post("/tenant/deactivate", headers=headers("A")).status_code == 200
        client.post("/tenant/activate", headers=headers("A"), json={"config": {}})
        assert client.post("/tenant/deactivate", headers=headers("A")).status_code == 200
        assert client.post("/tenant/deactivate", headers=headers("A")).status_code == 200


# --------------------------------------------------------------------------- 6. performance and concurrency
class TestPerformance:
    def test_the_same_order_arriving_many_times_at_once_makes_one_shipment(self, world):
        client, db, _ = world
        from app import logistics
        from app.config import parse_config

        db.upsert_tenant("A", "key-A", parse_config({}))
        logistics.ingest("A", "stock_level", "S", 1, {"sku": "SKU", "quantite_disponible": 100})
        payload = {"statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 1}], "latitude": 36.7, "longitude": 5.0}
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda v: logistics.ingest("A", "commande", "SAME", v, payload), [1] * 8))
        assert all(r["processed"] for r in results)
        assert len(db.list_shipments("A")) == 1 and len(db.list_events("A", "EXP-SAME")) == 1

    def test_many_orders_at_once_are_all_kept(self, world):
        client, db, _ = world
        from app import logistics
        from app.config import parse_config

        db.upsert_tenant("A", "key-A", parse_config({}))
        logistics.ingest("A", "stock_level", "S", 1, {"sku": "SKU", "quantite_disponible": 1000})

        def push(i):
            return logistics.ingest("A", "commande", f"C{i}", 1, {
                "statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 1}], "latitude": 36.7 + i / 1000, "longitude": 5.0})

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(push, range(60)))
        assert len(db.list_shipments("A")) == 60 and len(db.list_objects("A", "commande")) == 60

    def test_a_burst_of_notifications_is_answered_quickly(self, world):
        client, *_ = world
        client.post("/tenant/activate", headers=headers("A"), json={"config": {}})
        client.post("/exchange/notify", headers=headers("A"), json={
            "object_type": "stock_level", "external_id": "S", "version": 1, "payload": {"sku": "SKU", "quantite_disponible": 1000}})
        started = time.perf_counter()
        for i in range(40):
            r = client.post("/exchange/notify", headers=headers("A"), json={
                "object_type": "commande", "external_id": f"C{i}", "version": 1,
                "payload": {"statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": 1}], "adresse_livraison": "x"}})
            assert r.status_code == 200
        average = (time.perf_counter() - started) / 40
        assert average < 1.0  # the core gives an agent 10 seconds to answer; we stay far below

    def test_a_plan_of_50_stops_takes_less_than_10_seconds_through_the_api(self, world):
        client, db, _ = world
        from app import benchmark

        client.post("/tenant/activate", headers=headers("A"), json={"config": {
            "depot": DEPOT, "vehicles": [{"id": f"V{i}", "capacity": 40} for i in range(4)]}})
        client.post("/exchange/notify", headers=headers("A"), json={
            "object_type": "stock_level", "external_id": "S", "version": 1, "payload": {"sku": "SKU", "quantite_disponible": 10000}})
        points, demands, _ = benchmark.make_instance(50, seed=1)
        for i, ((lat, lon), qty) in enumerate(zip(points[1:], demands[1:])):
            client.post("/exchange/notify", headers=headers("A"), json={
                "object_type": "commande", "external_id": f"P{i}", "version": 1,
                "payload": {"statut": "confirmee", "lignes": [{"sku": "SKU", "quantite": qty}], "latitude": lat, "longitude": lon}})
        started = time.perf_counter()
        r = client.post("/tenant/A/plans", headers=headers("A"), json={"plan_date": "2026-11-08", "solver_seconds": 5})
        elapsed = time.perf_counter() - started
        plan = r.json()["data"]
        assert plan["stops_planned"] == 50 and plan["unassigned"] == []
        assert elapsed < 10 and plan["gain_pct"] >= 15
