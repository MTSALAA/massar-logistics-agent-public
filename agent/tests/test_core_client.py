"""Calls to the core: publishing `expedition`, pull fallback. The core is replaced by an httpx mock."""
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

KEY = "secret-key-of-A"


class FakeCore:
    """Records what the agent sends and answers like the real core's exchange routes."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.versions: dict[str, int] = {}
        self.objects = {"commande": [], "stock_level": []}
        self.fail_with: int | None = None
        self.unreachable = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("core is down")
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"detail": "boom"})
        kind = request.url.path.rsplit("/", 1)[-1]
        if request.method == "POST":
            body = request.read()
            import json

            external_id = json.loads(body)["external_id"]
            self.versions[external_id] = self.versions.get(external_id, 0) + 1
            return httpx.Response(200, json={"status": "ok", "data": {"id": "x", "version": self.versions[external_id]}, "error": None})
        return httpx.Response(200, json={"status": "ok", "data": self.objects.get(kind, []), "error": None})

    def posted(self):
        import json

        return [json.loads(r.read()) for r in self.requests if r.method == "POST"]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("CORE_URL", "http://core.test/")
    from app import core_client, db, logistics
    from app.config import parse_config

    core = FakeCore()
    monkeypatch.setattr(core_client, "transport", httpx.MockTransport(core))
    db.upsert_tenant("A", KEY, parse_config({}))
    return core, core_client, db, logistics


def make_shipment(logistics, ref="CMD-1"):
    logistics.ingest("A", "stock_level", "S1", 1, {"sku": "SKU-1", "quantite_disponible": 9})
    logistics.ingest(
        "A", "commande", ref, 1,
        {"statut": "confirmee", "lignes": [{"sku": "SKU-1", "quantite": 1}], "adresse_livraison": "1 rue A"},
    )
    return f"EXP-{ref}"


def test_publish_sends_the_expedition_with_the_tenant_credentials(world):
    core, client, db, logistics = world
    make_shipment(logistics)
    assert client.publish_pending("A") == {"published": 1, "failed": 0, "skipped": False}
    request = core.requests[0]
    assert (request.method, str(request.url)) == ("POST", "http://core.test/exchange/agent/expedition")
    assert request.headers["Authorization"] == f"Bearer {KEY}" and request.headers["X-Tenant-Id"] == "A"
    assert core.posted()[0] == {
        "external_id": "EXP-CMD-1",
        "payload": {
            "numero_expedition": "EXP-CMD-1",
            "reference_commande": "CMD-1",
            "transporteur": "Massar Logistique",
            "numero_suivi": db.get_shipment("A", "EXP-CMD-1")["tracking_number"],
            "statut": "preparation",
        },
    }
    shipment = db.get_shipment("A", "EXP-CMD-1")
    assert shipment["publish_pending"] == 0 and shipment["published_version"] == 1


def test_nothing_is_sent_twice(world):
    core, client, _, logistics = world
    make_shipment(logistics)
    client.publish_pending("A")
    assert client.publish_pending("A")["published"] == 0
    assert len(core.posted()) == 1


def test_every_status_change_is_published_with_the_same_id_so_the_core_versions_it(world):
    core, client, db, logistics = world
    shipment_id = make_shipment(logistics)
    client.publish_pending("A")
    logistics.change_status("A", shipment_id, "en_transit")
    client.publish_pending("A")
    logistics.change_status("A", shipment_id, "livree")
    client.publish_pending("A")
    assert [(p["external_id"], p["payload"]["statut"]) for p in core.posted()] == [
        ("EXP-CMD-1", "preparation"), ("EXP-CMD-1", "en_transit"), ("EXP-CMD-1", "livree"),
    ]
    assert db.get_shipment("A", shipment_id)["published_version"] == 3


@pytest.mark.parametrize("failure", ["http500", "http403", "down"])
def test_a_failing_core_never_raises_and_the_shipment_stays_pending(world, failure, caplog):
    core, client, db, logistics = world
    make_shipment(logistics)
    if failure == "down":
        core.unreachable = True
    else:
        core.fail_with = int(failure[4:])
    with caplog.at_level(logging.DEBUG):
        result = client.publish_pending("A")
    assert result["published"] == 0
    assert db.get_shipment("A", "EXP-CMD-1")["publish_pending"] == 1
    assert KEY not in caplog.text  # the tenant key is never logged
    core.fail_with, core.unreachable = None, False
    assert client.publish_pending("A")["published"] == 1  # retried later


def test_without_a_core_url_the_agent_works_offline(world, monkeypatch):
    core, client, db, logistics = world
    monkeypatch.delenv("CORE_URL")
    make_shipment(logistics)
    assert client.publish_pending("A")["skipped"] is True
    assert client.pull_from_core("A")["skipped"] is True
    assert core.requests == []
    assert db.get_shipment("A", "EXP-CMD-1")["publish_pending"] == 1


def test_a_deactivated_tenant_is_not_published(world):
    core, client, db, logistics = world
    make_shipment(logistics)
    db.purge_tenant("A")
    assert client.publish_pending("A")["skipped"] is True
    assert core.requests == []


# --------------------------------------------------------------------------- pull
def test_pull_reads_stock_first_then_orders_and_applies_the_rules(world):
    core, client, db, logistics = world
    core.objects["stock_level"] = [{"external_id": "S1", "version": 1, "payload": {"sku": "SKU-1", "quantite_disponible": 5}}]
    core.objects["commande"] = [
        {
            "external_id": "CMD-7", "version": 2,
            "payload": {"statut": "confirmee", "lignes": [{"sku": "SKU-1", "quantite": 2}], "adresse_livraison": "9 rue B"},
        },
        {"external_id": "CMD-BAD", "version": 1, "payload": "not an object"},  # skipped, no crash
    ]
    assert client.pull_from_core("A") == {"pulled": 2, "skipped": False}
    assert [r.url.path for r in core.requests] == ["/exchange/agent/stock_level", "/exchange/agent/commande"]
    assert db.get_shipment_by_order("A", "CMD-7")["status"] == "preparation"
    assert db.get_object("A", "commande", "CMD-7")["version"] == 2


def test_pull_survives_one_failing_type_and_a_dead_core(world):
    core, client, _, _ = world
    core.fail_with = 500
    assert client.pull_from_core("A")["pulled"] == 0
    core.fail_with, core.unreachable = None, True
    assert client.pull_from_core("A")["pulled"] == 0


def test_sync_pulls_then_publishes(world):
    core, client, db, _ = world
    core.objects["stock_level"] = [{"external_id": "S1", "version": 1, "payload": {"sku": "SKU-1", "quantite_disponible": 5}}]
    core.objects["commande"] = [
        {"external_id": "CMD-8", "version": 1,
         "payload": {"statut": "confirmee", "lignes": [{"sku": "SKU-1", "quantite": 1}], "latitude": 36.7, "longitude": 3.0}},
    ]
    result = client.sync("A")
    assert result["pull"]["pulled"] == 2 and result["publish"]["published"] == 1
    assert core.posted()[0]["external_id"] == "EXP-CMD-8"


# --------------------------------------------------------------------------- through the HTTP layer
def test_a_pushed_order_is_published_after_the_reply(world):
    from app.main import app

    core, _, db, logistics = world
    logistics.ingest("A", "stock_level", "S1", 1, {"sku": "SKU-1", "quantite_disponible": 9})
    http = TestClient(app)
    r = http.post(
        "/exchange/notify",
        headers={"X-Tenant-Id": "A", "Authorization": f"Bearer {KEY}"},
        json={
            "object_type": "commande", "external_id": "CMD-9", "version": 1,
            "payload": {"statut": "confirmee", "lignes": [{"sku": "SKU-1", "quantite": 1}], "adresse_livraison": "3 rue C"},
        },
    )
    assert r.json()["data"] == {"processed": True, "decision": "shipped"}
    assert [p["external_id"] for p in core.posted()] == ["EXP-CMD-9"]
    assert db.get_shipment("A", "EXP-CMD-9")["publish_pending"] == 0


def test_activation_takes_the_objects_already_on_the_core(world):
    from app.main import app

    core, _, db, _ = world
    core.objects["stock_level"] = [{"external_id": "S1", "version": 1, "payload": {"sku": "SKU-1", "quantite_disponible": 5}}]
    core.objects["commande"] = [
        {"external_id": "CMD-5", "version": 1,
         "payload": {"statut": "confirmee", "lignes": [{"sku": "SKU-1", "quantite": 1}], "adresse_livraison": "2 rue D"}},
    ]
    r = TestClient(app).post(
        "/tenant/activate", json={"config": {}}, headers={"X-Tenant-Id": "N", "Authorization": "Bearer key-N"}
    )
    assert r.json()["data"] == {"activated": True}
    assert db.get_shipment_by_order("N", "CMD-5") is not None
    assert core.requests[0].headers["Authorization"] == "Bearer key-N"
