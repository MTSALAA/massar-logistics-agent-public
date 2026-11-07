"""Operations API: shipments, status changes, orders, alerts, sync."""
import pytest
from fastapi.testclient import TestClient


def auth(tenant="A", key=None):
    return {"X-Tenant-Id": tenant, "Authorization": f"Bearer {key or 'key-' + tenant}"}


@pytest.fixture()
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("CORE_URL", raising=False)
    from app.main import app

    client = TestClient(app)
    for tenant in ("A", "B"):
        client.post("/tenant/activate", json={"config": {}}, headers=auth(tenant))
    return client


def push(http, object_type, external_id, payload, tenant="A", version=1):
    r = http.post(
        "/exchange/notify",
        headers=auth(tenant),
        json={"object_type": object_type, "external_id": external_id, "version": version, "payload": payload},
    )
    assert r.status_code == 200
    return r.json()["data"]


def ship_one(http, tenant="A", ref="CMD-1"):
    push(http, "stock_level", "S1", {"sku": "SKU-1", "quantite_disponible": 9}, tenant)
    push(http, "commande", ref, {
        "statut": "confirmee", "lignes": [{"sku": "SKU-1", "quantite": 1}], "adresse_livraison": "1 rue A",
    }, tenant)
    return f"EXP-{ref}"


def assert_envelope(body):
    assert set(body) == {"status", "data", "error"}


def test_list_and_read_shipments(http):
    shipment_id = ship_one(http)
    r = http.get("/tenant/A/shipments", headers=auth())
    assert r.status_code == 200
    assert_envelope(r.json())
    [shipment] = r.json()["data"]
    assert shipment["shipment_id"] == shipment_id and shipment["status"] == "preparation"
    assert shipment["published"] == {"pending": True, "version": None}
    assert [h["status"] for h in shipment["history"]] == ["preparation"]
    one = http.get(f"/tenant/A/shipments/{shipment_id}", headers=auth()).json()["data"]
    assert one["tracking_number"] == shipment["tracking_number"]


def test_status_changes_follow_the_life_cycle(http):
    shipment_id = ship_one(http)
    url = f"/tenant/A/shipments/{shipment_id}/status"
    r = http.post(url, headers=auth(), json={"status": "en_transit"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "en_transit"
    r = http.post(url, headers=auth(), json={"status": "livree"})
    assert r.json()["data"]["status"] == "livree"
    assert [h["status"] for h in r.json()["data"]["history"]] == ["preparation", "en_transit", "livree"]


def test_a_failed_delivery_needs_a_reason(http):
    shipment_id = ship_one(http)
    url = f"/tenant/A/shipments/{shipment_id}/status"
    http.post(url, headers=auth(), json={"status": "en_transit"})
    r = http.post(url, headers=auth(), json={"status": "retour"})
    assert r.status_code == 422 and "reason" in r.json()["error"]
    r = http.post(url, headers=auth(), json={"status": "retour", "reason": "Customer absent"})
    assert r.status_code == 200 and r.json()["data"]["failure_reason"] == "Customer absent"


@pytest.mark.parametrize(
    "body, code",
    [({"status": "livree"}, 409), ({"status": "nope"}, 422), ({}, 422), ({"status": 3}, 422)],
)
def test_invalid_status_requests_keep_the_envelope(http, body, code):
    shipment_id = ship_one(http)
    r = http.post(f"/tenant/A/shipments/{shipment_id}/status", headers=auth(), json=body)
    assert r.status_code == code
    assert_envelope(r.json())
    assert r.json()["status"] == "error"


def test_unknown_shipment_is_404(http):
    assert http.get("/tenant/A/shipments/EXP-NOPE", headers=auth()).status_code == 404
    r = http.post("/tenant/A/shipments/EXP-NOPE/status", headers=auth(), json={"status": "en_transit"})
    assert r.status_code == 404 and r.json()["status"] == "error"


@pytest.mark.parametrize("path", ["shipments", "orders", "alerts"])
def test_routes_need_the_right_key_and_the_right_tenant(http, path):
    ship_one(http)
    assert http.get(f"/tenant/A/{path}").status_code == 400  # no headers at all
    assert http.get(f"/tenant/A/{path}", headers=auth("A", "wrong")).status_code == 401
    assert http.get(f"/tenant/A/{path}", headers=auth("B")).status_code == 403  # B's key on A's path
    assert http.get(f"/tenant/A/{path}", headers=auth("A")).status_code == 200


def test_one_company_never_sees_or_moves_another_companys_shipments(http):
    shipment_id = ship_one(http, "A")
    assert http.get("/tenant/B/shipments", headers=auth("B")).json()["data"] == []
    assert http.get(f"/tenant/B/shipments/{shipment_id}", headers=auth("B")).status_code == 404
    r = http.post(f"/tenant/B/shipments/{shipment_id}/status", headers=auth("B"), json={"status": "en_transit"})
    assert r.status_code == 404
    assert http.get(f"/tenant/A/shipments/{shipment_id}", headers=auth("A")).json()["data"]["status"] == "preparation"


def test_orders_and_alerts_explain_what_was_decided(http):
    push(http, "commande", "CMD-NOSTOCK", {
        "statut": "confirmee", "lignes": [{"sku": "SKU-9", "quantite": 1}], "adresse_livraison": "1 rue A",
    })
    push(http, "commande", "CMD-NOADDR", {"statut": "confirmee", "lignes": [{"sku": "SKU-9", "quantite": 1}]})
    orders = {o["order_ref"]: o for o in http.get("/tenant/A/orders", headers=auth()).json()["data"]}
    assert orders["CMD-NOSTOCK"]["decision"] == "blocked" and orders["CMD-NOADDR"]["decision"] == "blocked"
    alerts = {a["order_ref"]: a["kind"] for a in http.get("/tenant/A/alerts", headers=auth()).json()["data"]}
    assert alerts == {"CMD-NOSTOCK": "stock_unknown", "CMD-NOADDR": "address_missing"}
    assert http.get("/tenant/B/alerts", headers=auth("B")).json()["data"] == []


def test_resolved_alerts_are_hidden_unless_asked(http):
    located = {"statut": "confirmee", "lignes": [{"sku": "SKU-1", "quantite": 1}], "latitude": 36.7, "longitude": 3.05}
    push(http, "commande", "CMD-1", located)
    push(http, "stock_level", "S1", {"sku": "SKU-1", "quantite_disponible": 5})
    push(http, "commande", "CMD-1", located, version=2)
    assert http.get("/tenant/A/alerts", headers=auth()).json()["data"] == []
    history = http.get("/tenant/A/alerts?include_resolved=true", headers=auth()).json()["data"]
    assert [a["kind"] for a in history] == ["stock_unknown"] and history[0]["resolved_at"]


def test_sync_without_a_core_is_harmless(http):
    r = http.post("/tenant/A/sync", headers=auth())
    assert r.status_code == 200
    assert r.json()["data"]["pull"]["skipped"] is True


def test_deactivation_removes_shipments_orders_and_alerts(http):
    ship_one(http, "A")
    ship_one(http, "B", ref="CMD-B")
    assert http.post("/tenant/deactivate", headers=auth("A")).status_code == 200
    assert http.get("/tenant/A/shipments", headers=auth("A")).status_code == 404  # no longer activated
    from app import db

    assert db.count_rows("A") == 0 and db.count_rows("B") > 0
    assert len(http.get("/tenant/B/shipments", headers=auth("B")).json()["data"]) == 1
