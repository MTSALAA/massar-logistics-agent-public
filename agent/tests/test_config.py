"""Company configuration: defaults, validation, and the published JSON Schema."""
import pytest
from fastapi.testclient import TestClient

KEY = "key-" + "x"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    from app.main import app

    return TestClient(app)


def activate(client, config, tenant="A"):
    return client.post(
        "/tenant/activate",
        json={"config": config},
        headers={"X-Tenant-Id": tenant, "Authorization": f"Bearer {KEY}"},
    )


def test_empty_config_activates_with_every_default(client):
    from app import db

    # The core's web page subscribes without any config: this must work.
    assert activate(client, {}).status_code == 200
    config = db.get_config("A")
    assert config["depot"] is None
    assert config["vehicles"] == [{"id": "V1", "capacity": 1000.0}]
    assert config["working_hours"] == {"start": "08:00", "end": "17:00"}
    assert config["stop_duration_minutes"] == 10
    assert config["wait_for_stock"] is False
    assert config["utc_offset_minutes"] == 60


def test_a_full_valid_config_is_stored(client):
    from app import db

    config = {
        "depot": {"lat": 36.64, "lon": 4.9},
        "vehicles": [{"id": "van-1", "capacity": 40}, {"id": "van-2", "capacity": 25.5}],
        "working_hours": {"start": "07:30", "end": "18:00"},
        "stop_duration_minutes": 15,
        "wait_for_stock": True,
    }
    assert activate(client, config).status_code == 200
    stored = db.get_config("A")
    assert stored["depot"] == {"lat": 36.64, "lon": 4.9}
    assert [v["id"] for v in stored["vehicles"]] == ["van-1", "van-2"]
    assert stored["working_hours"] == {"start": "07:30", "end": "18:00"}
    assert stored["wait_for_stock"] is True


def test_vehicle_names_in_any_language_are_accepted(client):
    from app import db

    names = ["van-1", "Camion 2", "véhicule n°1".replace("°", ""), "شاحنة 3", "V.4/B"]
    assert activate(client, {"vehicles": [{"id": n, "capacity": 5} for n in names]}).status_code == 200
    assert [v["id"] for v in db.get_config("A")["vehicles"]] == names


def test_partial_config_keeps_defaults_for_the_rest(client):
    from app import db

    activate(client, {"working_hours": {"start": "09:00"}})
    assert db.get_config("A")["working_hours"] == {"start": "09:00", "end": "17:00"}


def test_unknown_fields_are_ignored_silently(client):
    from app import db

    r = activate(client, {"wait_for_stock": True, "something_new": 1, "depot": {"lat": 1, "lon": 2, "x": 3}})
    assert r.status_code == 200
    assert "something_new" not in db.get_config("A")


@pytest.mark.parametrize(
    "config, expected_in_message",
    [
        ({"depot": {"lat": 120, "lon": 4}}, "depot.lat"),
        ({"depot": {"lat": 36}}, "depot.lon"),
        ({"vehicles": []}, "vehicles"),
        ({"vehicles": [{"id": "a", "capacity": 0}]}, "capacity"),
        ({"vehicles": [{"id": "a", "capacity": -5}]}, "capacity"),
        ({"vehicles": [{"id": "a", "capacity": 10}, {"id": "a", "capacity": 20}]}, "unique"),
        ({"vehicles": [{"id": "", "capacity": 10}]}, "vehicles.0.id"),
        ({"working_hours": {"start": "8h"}}, "working_hours.start"),
        ({"working_hours": {"start": "25:00"}}, "working_hours.start"),
        ({"working_hours": {"start": "18:00", "end": "08:00"}}, "earlier"),
        ({"stop_duration_minutes": -1}, "stop_duration_minutes"),
        ({"stop_duration_minutes": "ten"}, "stop_duration_minutes"),
        ({"wait_for_stock": "yes"}, "wait_for_stock"),
        ({"vehicles": [{"id": 'x","status":"retour', "capacity": 10}]}, "vehicles.0.id"),
        ({"vehicles": [{"id": "<script>", "capacity": 10}]}, "vehicles.0.id"),
        ({"utc_offset_minutes": 900}, "utc_offset_minutes"),
        ({"utc_offset_minutes": 1.5}, "utc_offset_minutes"),
        ({"vehicles": [{"id": "a", "capacity": True}]}, "capacity"),
    ],
)
def test_invalid_config_is_refused_in_the_envelope_and_stores_nothing(client, config, expected_in_message):
    from app import db

    r = activate(client, config)
    body = r.json()
    assert r.status_code == 422
    assert set(body) == {"status", "data", "error"} and body["status"] == "error" and body["data"] is None
    assert expected_in_message in body["error"]
    assert db.get_tenant("A") is None


def test_invalid_reactivation_keeps_the_previous_config(client):
    from app import db

    activate(client, {"wait_for_stock": True})
    assert activate(client, {"stop_duration_minutes": -1}).status_code == 422
    assert db.get_config("A")["wait_for_stock"] is True


def test_config_schema_matches_the_model(client):
    body = client.get("/config-schema").json()
    assert body["status"] == "ok"
    schema = body["data"]
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {
        "depot",
        "vehicles",
        "working_hours",
        "stop_duration_minutes",
        "utc_offset_minutes",
        "wait_for_stock",
    }
    assert schema["properties"]["wait_for_stock"]["default"] is False
