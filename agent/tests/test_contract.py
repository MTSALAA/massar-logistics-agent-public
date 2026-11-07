"""Contract conformity tests: endpoints, response envelope, tenant isolation, purge."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    from app.main import app

    return TestClient(app)


def headers(tenant_id: str, key: str = "key-" + "x"):
    return {"X-Tenant-Id": tenant_id, "Authorization": f"Bearer {key}"}


def assert_envelope(body: dict):
    assert set(body.keys()) == {"status", "data", "error"}
    assert body["status"] in ("ok", "error")
    if body["status"] == "ok":
        assert body["error"] is None
    else:
        assert body["data"] is None and isinstance(body["error"], str)


def notify_body(object_type="commande", external_id="CMD-1"):
    return {
        "object_type": object_type,
        "external_id": external_id,
        "version": 1,
        "payload": {"numero_commande": external_id},
        "unknown_field": "must be ignored",
    }


def test_health_uses_envelope(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert_envelope(r.json())
    assert "uptime_seconds" in r.json()["data"]


def test_config_schema_is_json_schema_in_envelope(client):
    r = client.get("/config-schema")
    body = r.json()
    assert_envelope(body)
    assert body["data"]["type"] == "object"
    assert "properties" in body["data"]


def test_activate_requires_headers_and_keeps_envelope(client):
    r = client.post("/tenant/activate", json={"config": {}})
    assert r.status_code == 400
    assert_envelope(r.json())
    r = client.post("/tenant/activate", json={"config": {}}, headers={"X-Tenant-Id": "A"})
    assert r.status_code == 401
    assert_envelope(r.json())


def test_activate_then_status(client):
    r = client.post("/tenant/activate", json={"config": {"wait_for_stock": True}}, headers=headers("A"))
    assert r.status_code == 200
    assert r.json()["data"] == {"activated": True}
    r = client.get("/tenant/A/status")
    assert r.status_code == 200
    assert r.json()["data"]["state"] == "running"


def test_status_of_unknown_tenant_is_404_in_envelope(client):
    r = client.get("/tenant/unknown/status")
    assert r.status_code == 404
    assert_envelope(r.json())


def test_unknown_route_keeps_envelope(client):
    r = client.get("/does-not-exist")
    assert r.status_code == 404
    assert_envelope(r.json())


def test_invalid_body_keeps_envelope(client):
    client.post("/tenant/activate", json={"config": {}}, headers=headers("A"))
    r = client.post("/exchange/notify", json={"nope": 1}, headers=headers("A"))
    assert r.status_code == 422
    assert_envelope(r.json())


def test_exchange_notify_stores_consumed_objects_and_ignores_unknown_fields(client):
    from app import db

    client.post("/tenant/activate", json={"config": {}}, headers=headers("A"))
    r = client.post("/exchange/notify", json=notify_body(), headers=headers("A"))
    assert r.status_code == 200
    assert r.json()["data"]["processed"] is True
    assert len(db.list_objects("A", "commande")) == 1


def test_exchange_notify_ignores_types_we_do_not_consume(client):
    client.post("/tenant/activate", json={"config": {}}, headers=headers("A"))
    r = client.post("/exchange/notify", json=notify_body("facture", "FAC-1"), headers=headers("A"))
    assert r.status_code == 200
    assert r.json()["data"]["processed"] is False


def test_tenant_isolation(client):
    from app import db

    client.post("/tenant/activate", json={"config": {}}, headers=headers("A"))
    client.post("/tenant/activate", json={"config": {}}, headers=headers("B"))
    client.post("/exchange/notify", json=notify_body(external_id="CMD-A"), headers=headers("A"))
    assert db.list_objects("B", "commande") == []
    assert [o["external_id"] for o in db.list_objects("A", "commande")] == ["CMD-A"]


def test_notify_rejects_a_wrong_api_key(client):
    from app import db

    client.post("/tenant/activate", json={"config": {}}, headers=headers("A", "right-key"))
    r = client.post("/exchange/notify", json=notify_body(), headers=headers("A", "wrong-key"))
    assert r.status_code == 401
    assert_envelope(r.json())
    assert db.list_objects("A", "commande") == []


def test_one_tenant_key_does_not_open_another_tenant(client):
    from app import db

    client.post("/tenant/activate", json={"config": {}}, headers=headers("A", "key-of-A"))
    client.post("/tenant/activate", json={"config": {}}, headers=headers("B", "key-of-B"))
    r = client.post("/exchange/notify", json=notify_body(external_id="X"), headers=headers("B", "key-of-A"))
    assert r.status_code == 401
    assert db.list_objects("B", "commande") == []


def test_deactivate_with_a_wrong_key_deletes_nothing(client):
    from app import db

    client.post("/tenant/activate", json={"config": {}}, headers=headers("A", "right-key"))
    client.post("/exchange/notify", json=notify_body(), headers=headers("A", "right-key"))
    r = client.post("/tenant/deactivate", headers=headers("A", "wrong-key"))
    assert r.status_code == 401
    assert_envelope(r.json())
    assert db.count_rows("A") > 0


def test_deactivate_of_an_unknown_tenant_is_harmless(client):
    r = client.post("/tenant/deactivate", headers=headers("never-activated"))
    assert r.status_code == 200
    assert r.json()["data"] == {"deactivated": True, "data_purged": True}


def test_reactivation_replaces_the_key(client):
    client.post("/tenant/activate", json={"config": {}}, headers=headers("A", "old-key"))
    client.post("/tenant/activate", json={"config": {}}, headers=headers("A", "new-key"))
    assert client.post("/exchange/notify", json=notify_body(), headers=headers("A", "old-key")).status_code == 401
    assert client.post("/exchange/notify", json=notify_body(), headers=headers("A", "new-key")).status_code == 200


def test_deactivate_really_purges_the_tenant_only(client):
    from app import db

    client.post("/tenant/activate", json={"config": {}}, headers=headers("A"))
    client.post("/tenant/activate", json={"config": {}}, headers=headers("B"))
    client.post("/exchange/notify", json=notify_body(external_id="CMD-A"), headers=headers("A"))
    client.post("/exchange/notify", json=notify_body(external_id="CMD-B"), headers=headers("B"))

    r = client.post("/tenant/deactivate", headers=headers("A"))
    assert r.status_code == 200
    assert r.json()["data"] == {"deactivated": True, "data_purged": True}
    assert db.count_rows("A") == 0
    assert db.count_rows("B") > 0
    assert client.get("/tenant/A/status").status_code == 404
