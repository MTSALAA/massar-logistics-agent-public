"""Shipping decision and shipment life cycle (business rules, no HTTP)."""
import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("CORE_URL", raising=False)
    from app import db, logistics
    from app.config import parse_config

    def activate(tenant="A", **config):
        db.upsert_tenant(tenant, "key-" + tenant, parse_config(config))

    activate()
    return db, logistics, activate


def order(statut="confirmee", lignes=None, **extra):
    payload = {
        "numero_commande": "CMD-1",
        "statut": statut,
        "client": {"nom": "Client", "email": "c@example.com"},
        "lignes": lignes if lignes is not None else [{"sku": "SKU-1", "quantite": 2, "prix_unitaire_ht": 10}],
        "adresse_livraison": "12 rue des Freres, Bejaia",
    }
    payload.update(extra)
    return payload


def push_order(logistics, ref="CMD-1", version=1, tenant="A", **kwargs):
    payload = order(**kwargs)
    if payload.get("adresse_livraison") is None:
        del payload["adresse_livraison"]
    return logistics.ingest(tenant, "commande", ref, version, payload)


def push_stock(logistics, sku="SKU-1", qty=10, ext=None, tenant="A"):
    return logistics.ingest(
        tenant, "stock_level", ext or f"STK-{sku}", 1, {"sku": sku, "quantite_disponible": qty, "entrepot": "W1"}
    )


def open_kinds(db, tenant="A"):
    return sorted(a["kind"] for a in db.list_alerts(tenant))


# --------------------------------------------------------------------------- decision
def test_order_with_stock_and_address_becomes_a_shipment(env):
    db, logistics, _ = env
    push_stock(logistics, qty=5)
    result = push_order(logistics)
    assert result == {"processed": True, "decision": "shipped"}
    shipment = db.get_shipment_by_order("A", "CMD-1")
    assert shipment["shipment_id"] == "EXP-CMD-1"
    assert shipment["status"] == "preparation"
    assert shipment["carrier"] == "Massar Logistique"
    assert shipment["tracking_number"].startswith("MSR-")
    assert shipment["address"] == "12 rue des Freres, Bejaia"
    assert shipment["publish_pending"] == 1
    assert [e["status"] for e in db.list_events("A", "EXP-CMD-1")] == ["preparation"]
    assert db.list_alerts("A") == []


def test_no_stock_information_blocks_the_order_with_an_alert(env):
    db, logistics, _ = env
    assert push_order(logistics)["decision"] == "blocked"
    assert open_kinds(db) == ["stock_unknown"]
    assert db.get_shipment_by_order("A", "CMD-1") is None


def test_insufficient_stock_blocks_and_names_the_sku(env):
    db, logistics, _ = env
    push_stock(logistics, qty=1)
    assert push_order(logistics)["decision"] == "blocked"
    alert = db.list_alerts("A")[0]
    assert alert["kind"] == "stock_insufficient"
    assert "SKU-1" in alert["message"] and "needs 2" in alert["message"] and "available 1" in alert["message"]


def test_stock_is_summed_over_warehouses(env):
    db, logistics, _ = env
    push_stock(logistics, qty=1, ext="STK-W1")
    push_stock(logistics, qty=1, ext="STK-W2")
    assert push_order(logistics)["decision"] == "shipped"


def test_every_line_needs_stock(env):
    db, logistics, _ = env
    push_stock(logistics, "SKU-1", 5)
    lines = [{"sku": "SKU-1", "quantite": 1}, {"sku": "SKU-2", "quantite": 1}]
    assert push_order(logistics, lignes=lines)["decision"] == "blocked"
    push_stock(logistics, "SKU-2", 1)
    assert db.get_shipment_by_order("A", "CMD-1") is None  # blocked orders wait for a new version
    assert push_order(logistics, version=2, lignes=lines)["decision"] == "shipped"
    assert db.list_alerts("A") == []


def test_wait_for_stock_holds_the_order_then_ships_when_stock_arrives(env):
    db, logistics, activate = env
    activate(wait_for_stock=True)
    assert push_order(logistics)["decision"] == "held"
    assert open_kinds(db) == ["stock_unknown"]
    push_stock(logistics, qty=1)
    assert db.get_decision("A", "CMD-1")["decision"] == "held"  # still not enough
    assert open_kinds(db) == ["stock_insufficient"]
    push_stock(logistics, qty=5)
    assert db.get_decision("A", "CMD-1")["decision"] == "shipped"
    assert db.get_shipment_by_order("A", "CMD-1")["status"] == "preparation"
    assert db.list_alerts("A") == []


def test_missing_address_blocks_until_a_new_version_brings_one(env):
    db, logistics, _ = env
    push_stock(logistics)
    assert push_order(logistics, adresse_livraison=None)["decision"] == "blocked"
    assert open_kinds(db) == ["address_missing"]
    assert push_order(logistics, version=2)["decision"] == "shipped"
    assert db.list_alerts("A") == []
    assert [a["kind"] for a in db.list_alerts("A", open_only=False)] == ["address_missing"]


def test_invalid_lines_block_the_order(env):
    db, logistics, _ = env
    for bad in ([], [{"sku": "SKU-1", "quantite": 0}], [{"sku": "SKU-1"}], [{"quantite": 2}], "nope"):
        result = push_order(logistics, version=db.get_object("A", "commande", "CMD-1")["version"] + 1
                            if db.get_object("A", "commande", "CMD-1") else 1, lignes=bad)
        assert result["decision"] == "blocked"
    assert open_kinds(db) == ["order_invalid"]


def test_unconfirmed_order_waits_without_alert_then_ships(env):
    db, logistics, _ = env
    push_stock(logistics)
    assert push_order(logistics, statut="en_attente")["decision"] == "waiting"
    assert db.list_alerts("A") == []
    assert push_order(logistics, version=2)["decision"] == "shipped"


def test_orders_already_shipped_or_invoiced_are_closed(env):
    db, logistics, _ = env
    push_stock(logistics)
    assert push_order(logistics, statut="facturee")["decision"] == "closed"
    assert db.get_shipment_by_order("A", "CMD-1") is None


def test_cancelled_order_before_pickup_returns_the_shipment(env):
    db, logistics, _ = env
    push_stock(logistics)
    push_order(logistics)
    assert push_order(logistics, version=2, statut="annulee")["decision"] == "cancelled"
    shipment = db.get_shipment_by_order("A", "CMD-1")
    assert shipment["status"] == "retour"
    assert shipment["failure_reason"] == "Order cancelled before pick-up"


def test_cancelled_order_already_on_the_road_keeps_its_shipment(env):
    db, logistics, _ = env
    push_stock(logistics)
    push_order(logistics)
    logistics.change_status("A", "EXP-CMD-1", "en_transit")
    push_order(logistics, version=2, statut="annulee")
    assert db.get_shipment_by_order("A", "CMD-1")["status"] == "en_transit"


def test_the_same_order_twice_makes_one_shipment(env):
    db, logistics, _ = env
    push_stock(logistics)
    push_order(logistics)
    push_order(logistics, version=2)
    assert len(db.list_shipments("A")) == 1
    assert len(db.list_events("A", "EXP-CMD-1")) == 1


def test_an_older_version_arriving_late_is_ignored(env):
    db, logistics, _ = env
    push_stock(logistics)
    push_order(logistics, version=3, statut="en_attente")
    assert push_order(logistics, version=2) == {"processed": False, "stale": True}
    assert db.get_decision("A", "CMD-1")["decision"] == "waiting"


def test_stock_of_another_company_does_not_help(env):
    db, logistics, activate = env
    activate("B")
    push_stock(logistics, qty=50, tenant="B")
    assert push_order(logistics)["decision"] == "blocked"
    assert db.list_shipments("B") == []


# --------------------------------------------------------------------------- address
@pytest.mark.parametrize(
    "payload, text, coords",
    [
        ({"adresse_livraison": " 5 rue X "}, "5 rue X", None),
        ({"client": {"adresse": "Avenue Y, Alger"}}, "Avenue Y, Alger", None),
        ({"adresse_livraison": {"rue": "1 rue Z", "code_postal": "06000", "ville": "Bejaia"}}, "1 rue Z, 06000, Bejaia", None),
        ({"adresse_livraison": {"ville": "Setif", "lat": 36.19, "lon": 5.41}}, "Setif", (36.19, 5.41)),
        ({"latitude": 36.7, "longitude": 3.05}, None, (36.7, 3.05)),
        ({"adresse_livraison": "A", "client": {"adresse": "B"}}, "A", None),
        ({"latitude": 120, "longitude": 3}, None, None),
        ({"latitude": True, "longitude": 3}, None, None),
        ({"latitude": 36.7}, None, None),
        ({"adresse_livraison": "   "}, None, None),
        ({}, None, None),
    ],
)
def test_delivery_address_formats(env, payload, text, coords):
    _, logistics, _ = env
    address = logistics.delivery_address(payload)
    assert address.text == text
    assert ((address.lat, address.lon) if address.lat is not None else None) == coords
    assert address.known is bool(text or coords)


# --------------------------------------------------------------------------- life cycle
def shipped(env):
    db, logistics, _ = env
    push_stock(logistics)
    push_order(logistics)
    return db, logistics


def test_shipment_goes_through_its_life_cycle_with_a_timestamped_history(env):
    db, logistics = shipped(env)
    logistics.change_status("A", "EXP-CMD-1", "en_transit")
    logistics.change_status("A", "EXP-CMD-1", "livree")
    history = db.list_events("A", "EXP-CMD-1")
    assert [e["status"] for e in history] == ["preparation", "en_transit", "livree"]
    assert history == sorted(history, key=lambda e: e["at"])
    assert all(e["at"].endswith("+00:00") for e in history)  # ISO 8601 UTC
    assert db.get_shipment("A", "EXP-CMD-1")["publish_pending"] == 1


def test_a_failed_delivery_records_the_reason(env):
    db, logistics = shipped(env)
    logistics.change_status("A", "EXP-CMD-1", "en_transit")
    logistics.change_status("A", "EXP-CMD-1", "retour", "  Customer absent  ")
    shipment = db.get_shipment("A", "EXP-CMD-1")
    assert shipment["status"] == "retour" and shipment["failure_reason"] == "Customer absent"
    assert db.list_events("A", "EXP-CMD-1")[-1]["note"] == "Customer absent"


@pytest.mark.parametrize(
    "path, bad, code",
    [
        (["en_transit"], "preparation", 409),
        ([], "livree", 409),
        (["en_transit", "livree"], "retour", 409),
        (["en_transit"], "en_transit", 409),
        ([], "teleportee", 422),
        ([], "retour", 422),  # a return needs a reason
    ],
)
def test_invalid_status_changes_are_refused_and_change_nothing(env, path, bad, code):
    db, logistics = shipped(env)
    for step in path:
        logistics.change_status("A", "EXP-CMD-1", step)
    before = db.list_events("A", "EXP-CMD-1")
    with pytest.raises(logistics.TransitionError) as caught:
        logistics.change_status("A", "EXP-CMD-1", bad)
    assert caught.value.code == code
    assert db.list_events("A", "EXP-CMD-1") == before


def test_unknown_shipment_and_other_company_shipment_are_not_found(env):
    db, logistics = shipped(env)
    env[2]("B")
    for tenant, shipment_id in (("A", "EXP-NOPE"), ("B", "EXP-CMD-1")):
        with pytest.raises(logistics.TransitionError) as caught:
            logistics.change_status(tenant, shipment_id, "en_transit")
        assert caught.value.code == 404


def test_purge_deletes_orders_shipments_history_decisions_and_alerts(env):
    db, logistics, activate = env
    activate("B")
    push_stock(logistics)
    push_order(logistics)
    push_order(logistics, ref="CMD-2", version=1, lignes=[{"sku": "NOPE", "quantite": 1}])  # leaves an alert
    push_stock(logistics, qty=3, tenant="B")
    assert db.count_rows("A") > 6
    db.purge_tenant("A")
    assert db.count_rows("A") == 0
    assert db.count_rows("B") > 0
    for table in db.TENANT_TABLES:
        with db.connect() as c:
            assert c.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = 'A'").fetchone()[0] == 0
