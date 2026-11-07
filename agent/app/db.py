"""SQLite storage. Every table carries a tenant_id column (strict per-company isolation).

RULE: every new table MUST have a tenant_id column and MUST be listed in TENANT_TABLES,
so that purge_tenant() really deletes a company's data on /tenant/deactivate.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

# Tables holding tenant data. Order matters only if foreign keys are added later.
TENANT_TABLES = (
    "objects", "order_decisions", "shipments", "shipment_events", "alerts",
    "geocache", "route_plans", "route_stops", "route_paths", "ui_access", "tenants",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id    TEXT PRIMARY KEY,
    api_key      TEXT NOT NULL,
    config_json  TEXT NOT NULL,
    activated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS objects (
    tenant_id    TEXT NOT NULL,
    object_type  TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    version      INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, object_type, external_id)
);
CREATE TABLE IF NOT EXISTS order_decisions (
    tenant_id  TEXT NOT NULL,
    order_ref  TEXT NOT NULL,
    decision   TEXT NOT NULL,
    reason     TEXT,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, order_ref)
);
CREATE TABLE IF NOT EXISTS shipments (
    tenant_id         TEXT NOT NULL,
    shipment_id       TEXT NOT NULL,
    order_ref         TEXT NOT NULL,
    status            TEXT NOT NULL,
    carrier           TEXT NOT NULL,
    tracking_number   TEXT NOT NULL,
    address           TEXT,
    lat               REAL,
    lon               REAL,
    failure_reason    TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    publish_pending   INTEGER NOT NULL DEFAULT 1,
    published_version INTEGER,
    earliest_date     TEXT,
    PRIMARY KEY (tenant_id, shipment_id)
);
CREATE TABLE IF NOT EXISTS shipment_events (
    tenant_id   TEXT NOT NULL,
    shipment_id TEXT NOT NULL,
    status      TEXT NOT NULL,
    at          TEXT NOT NULL,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    tenant_id   TEXT NOT NULL,
    order_ref   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    notified_at TEXT,
    PRIMARY KEY (tenant_id, order_ref, kind)
);
CREATE TABLE IF NOT EXISTS ui_access (
    tenant_id  TEXT NOT NULL,
    role       TEXT NOT NULL,
    salt       TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, role)
);
CREATE TABLE IF NOT EXISTS geocache (
    tenant_id TEXT NOT NULL,
    query_key TEXT NOT NULL,
    lat       REAL,
    lon       REAL,
    found     INTEGER NOT NULL,
    cached_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, query_key)
);
CREATE TABLE IF NOT EXISTS route_plans (
    tenant_id          TEXT NOT NULL,
    plan_id            TEXT NOT NULL,
    plan_date          TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    summary_json       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, plan_id)
);
CREATE TABLE IF NOT EXISTS route_paths (
    tenant_id     TEXT NOT NULL,
    plan_id       TEXT NOT NULL,
    vehicle_id    TEXT NOT NULL,
    geometry_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, plan_id, vehicle_id)
);
CREATE TABLE IF NOT EXISTS route_stops (
    tenant_id       TEXT NOT NULL,
    plan_id         TEXT NOT NULL,
    vehicle_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    shipment_id     TEXT NOT NULL,
    order_ref       TEXT NOT NULL,
    address         TEXT,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    demand          INTEGER NOT NULL,
    eta             TEXT NOT NULL,
    leg_distance_m  INTEGER NOT NULL,
    leg_duration_s  INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, plan_id, vehicle_id, seq)
);
"""


# Columns added after the first release: older database files get them when first opened.
MIGRATIONS = (("shipments", "earliest_date", "TEXT"), ("alerts", "notified_at", "TEXT"))


def _migrate(conn) -> None:
    for table, column, kind in MIGRATIONS:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def _path() -> str:
    # Read at call time so tests can point the agent to a temporary file.
    return os.getenv("DATABASE_PATH", "/data/agent.db")


@contextmanager
def connect():
    path = _path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    """Current time, ISO 8601 UTC (data contract: dates are ISO 8601 UTC)."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------
def upsert_tenant(tenant_id: str, api_key: str, config: dict) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO tenants (tenant_id, api_key, config_json, activated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET api_key=excluded.api_key, "
            "config_json=excluded.config_json, activated_at=excluded.activated_at",
            (tenant_id, api_key, json.dumps(config), now()),
        )


def get_tenant(tenant_id: str):
    with connect() as c:
        row = c.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def list_tenant_ids() -> list[str]:
    with connect() as c:
        return [r["tenant_id"] for r in c.execute("SELECT tenant_id FROM tenants")]


def get_config(tenant_id: str) -> dict | None:
    """The validated configuration of a company, defaults included (None if not activated)."""
    tenant = get_tenant(tenant_id)
    return json.loads(tenant["config_json"]) if tenant else None


def purge_tenant(tenant_id: str) -> None:
    """Really delete every row of this company (contract: /tenant/deactivate)."""
    with connect() as c:
        # The table names come from this module's own constant, never from a request; the value is a parameter.
        for table in TENANT_TABLES:
            c.execute(f"DELETE FROM {table} WHERE tenant_id = ?", (tenant_id,))  # nosec B608


def count_rows(tenant_id: str) -> int:
    """Total rows held for one company, across all tenant tables (used by isolation tests)."""
    total = 0
    with connect() as c:
        for table in TENANT_TABLES:
            total += c.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]  # nosec B608
    return total


# ---------------------------------------------------------------------------
# Objects received from the core (commande, stock_level)
# ---------------------------------------------------------------------------
def save_object(tenant_id: str, object_type: str, external_id: str, version: int, payload: dict) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO objects (tenant_id, object_type, external_id, version, payload_json, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, object_type, external_id) DO UPDATE SET "
            "version=excluded.version, payload_json=excluded.payload_json, received_at=excluded.received_at",
            (tenant_id, object_type, external_id, version, json.dumps(payload), now()),
        )


def _object_row(r) -> dict:
    return {"external_id": r["external_id"], "version": r["version"], "payload": json.loads(r["payload_json"])}


def list_objects(tenant_id: str, object_type: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT external_id, version, payload_json FROM objects WHERE tenant_id = ? AND object_type = ?",
            (tenant_id, object_type),
        ).fetchall()
    return [_object_row(r) for r in rows]


def get_object(tenant_id: str, object_type: str, external_id: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT external_id, version, payload_json FROM objects "
            "WHERE tenant_id = ? AND object_type = ? AND external_id = ?",
            (tenant_id, object_type, external_id),
        ).fetchone()
    return _object_row(row) if row else None


# ---------------------------------------------------------------------------
# Decisions and alerts
# ---------------------------------------------------------------------------
def set_decision(tenant_id: str, order_ref: str, decision: str, reason: str | None = None) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO order_decisions (tenant_id, order_ref, decision, reason, decided_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, order_ref) DO UPDATE SET decision=excluded.decision, "
            "reason=excluded.reason, decided_at=excluded.decided_at",
            (tenant_id, order_ref, decision, reason, now()),
        )


def get_decision(tenant_id: str, order_ref: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT order_ref, decision, reason, decided_at FROM order_decisions "
            "WHERE tenant_id = ? AND order_ref = ?",
            (tenant_id, order_ref),
        ).fetchone()
    return dict(row) if row else None


def list_decisions(tenant_id: str, decision: str | None = None) -> list[dict]:
    sql = "SELECT order_ref, decision, reason, decided_at FROM order_decisions WHERE tenant_id = ?"
    args: list = [tenant_id]
    if decision:
        sql += " AND decision = ?"
        args.append(decision)
    with connect() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY order_ref", args)]


def raise_alert(tenant_id: str, order_ref: str, kind: str, message: str) -> None:
    """One open alert per (order, kind): raising it again only refreshes the message."""
    with connect() as c:
        c.execute(
            "INSERT INTO alerts (tenant_id, order_ref, kind, message, created_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(tenant_id, order_ref, kind) DO UPDATE SET message=excluded.message, "
            "created_at=CASE WHEN alerts.resolved_at IS NULL THEN alerts.created_at ELSE excluded.created_at END, "
            "notified_at=CASE WHEN alerts.resolved_at IS NULL THEN alerts.notified_at ELSE NULL END, "
            "resolved_at=NULL",
            (tenant_id, order_ref, kind, message, now()),
        )


def resolve_alerts(tenant_id: str, order_ref: str, keep_kind: str | None = None) -> None:
    """Close the open alerts of an order (except `keep_kind`, the one still true)."""
    with connect() as c:
        c.execute(
            "UPDATE alerts SET resolved_at = ? WHERE tenant_id = ? AND order_ref = ? "
            "AND resolved_at IS NULL AND kind != ?",
            (now(), tenant_id, order_ref, keep_kind or ""),
        )


def list_alerts(tenant_id: str, open_only: bool = True) -> list[dict]:
    sql = "SELECT order_ref, kind, message, created_at, resolved_at FROM alerts WHERE tenant_id = ?"
    if open_only:
        sql += " AND resolved_at IS NULL"
    with connect() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY created_at, order_ref", (tenant_id,))]


# ---------------------------------------------------------------------------
# Shipments and their history
# ---------------------------------------------------------------------------
def create_shipment(tenant_id: str, shipment: dict) -> bool:
    """Insert a shipment in 'preparation' with its first event. False if it already exists."""
    stamp = now()
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO shipments (tenant_id, shipment_id, order_ref, status, carrier, tracking_number, "
            "address, lat, lon, created_at, updated_at, publish_pending, earliest_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                tenant_id, shipment["shipment_id"], shipment["order_ref"], shipment["status"], shipment["carrier"],
                shipment["tracking_number"], shipment.get("address"), shipment.get("lat"), shipment.get("lon"),
                stamp, stamp, shipment.get("earliest_date"),
            ),
        )
        if cur.rowcount == 0:
            return False
        c.execute(
            "INSERT INTO shipment_events (tenant_id, shipment_id, status, at, note) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, shipment["shipment_id"], shipment["status"], stamp, shipment.get("note")),
        )
    return True


def get_shipment(tenant_id: str, shipment_id: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM shipments WHERE tenant_id = ? AND shipment_id = ?", (tenant_id, shipment_id)
        ).fetchone()
    return dict(row) if row else None


def get_shipment_by_order(tenant_id: str, order_ref: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM shipments WHERE tenant_id = ? AND order_ref = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (tenant_id, order_ref),
        ).fetchone()
    return dict(row) if row else None


def list_shipments(tenant_id: str) -> list[dict]:
    with connect() as c:
        return [
            dict(r)
            for r in c.execute("SELECT * FROM shipments WHERE tenant_id = ? ORDER BY created_at, shipment_id", (tenant_id,))
        ]


def change_shipment_status(
    tenant_id: str, shipment_id: str, status: str, failure_reason: str | None = None, note: str | None = None
) -> None:
    """Record the new status with a timestamped event; the shipment must be published again."""
    stamp = now()
    with connect() as c:
        c.execute(
            "UPDATE shipments SET status = ?, failure_reason = ?, updated_at = ?, publish_pending = 1 "
            "WHERE tenant_id = ? AND shipment_id = ?",
            (status, failure_reason, stamp, tenant_id, shipment_id),
        )
        c.execute(
            "INSERT INTO shipment_events (tenant_id, shipment_id, status, at, note) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, shipment_id, status, stamp, note or failure_reason),
        )


def list_events(tenant_id: str, shipment_id: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT status, at, note FROM shipment_events WHERE tenant_id = ? AND shipment_id = ? ORDER BY rowid",
            (tenant_id, shipment_id),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_published(tenant_id: str, shipment_id: str, version: int | None, updated_at: str) -> None:
    """The core has the state we sent. Pending stays on if the shipment changed meanwhile."""
    with connect() as c:
        c.execute(
            "UPDATE shipments SET published_version = ?, publish_pending = CASE WHEN updated_at = ? THEN 0 ELSE 1 END "
            "WHERE tenant_id = ? AND shipment_id = ?",
            (version, updated_at, tenant_id, shipment_id),
        )


def list_pending_publication(tenant_id: str) -> list[dict]:
    with connect() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM shipments WHERE tenant_id = ? AND publish_pending = 1 ORDER BY created_at",
                (tenant_id,),
            )
        ]


# ---------------------------------------------------------------------------
# Geocoding cache and manual locations
# ---------------------------------------------------------------------------
def geocache_get(tenant_id: str, query_key: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT lat, lon, found FROM geocache WHERE tenant_id = ? AND query_key = ?", (tenant_id, query_key)
        ).fetchone()
    return dict(row) if row else None


def geocache_put(tenant_id: str, query_key: str, lat: float | None, lon: float | None, found: bool) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO geocache (tenant_id, query_key, lat, lon, found, cached_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, query_key) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, "
            "found=excluded.found, cached_at=excluded.cached_at",
            (tenant_id, query_key, lat, lon, 1 if found else 0, now()),
        )


def set_shipment_location(tenant_id: str, shipment_id: str, lat: float, lon: float) -> None:
    with connect() as c:
        c.execute(
            "UPDATE shipments SET lat = ?, lon = ? WHERE tenant_id = ? AND shipment_id = ?",
            (lat, lon, tenant_id, shipment_id),
        )


# ---------------------------------------------------------------------------
# Route plans
# ---------------------------------------------------------------------------
def save_plan(tenant_id: str, plan_date: str, summary: dict, stops: list[dict], paths: dict | None = None) -> str:
    """Store a plan, its stops and (when known) the road line of each vehicle. Returns the plan id (P1, P2, ...)."""
    with connect() as c:
        count = c.execute("SELECT COUNT(*) FROM route_plans WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
        plan_id = f"P{count + 1}"
        summary = {**summary, "plan_id": plan_id}
        c.execute(
            "INSERT INTO route_plans (tenant_id, plan_id, plan_date, created_at, summary_json) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, plan_id, plan_date, now(), json.dumps(summary)),
        )
        for vehicle_id, line in (paths or {}).items():
            c.execute(
                "INSERT INTO route_paths (tenant_id, plan_id, vehicle_id, geometry_json) VALUES (?, ?, ?, ?)",
                (tenant_id, plan_id, vehicle_id, json.dumps(line, separators=(",", ":"))),
            )
        for s in stops:
            c.execute(
                "INSERT INTO route_stops (tenant_id, plan_id, vehicle_id, seq, shipment_id, order_ref, address, lat, lon, "
                "demand, eta, leg_distance_m, leg_duration_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id, plan_id, s["vehicle_id"], s["seq"], s["shipment_id"], s["order_ref"], s.get("address"),
                    s["lat"], s["lon"], s["demand"], s["eta"], s["leg_distance_m"], s["leg_duration_s"],
                ),
            )
    return plan_id


def _plan_row(c, tenant_id: str, row) -> dict:
    plan = json.loads(row["summary_json"])
    plan.update(plan_date=row["plan_date"], created_at=row["created_at"])
    stops = c.execute(
        "SELECT vehicle_id, seq, shipment_id, order_ref, address, lat, lon, demand, eta, leg_distance_m, leg_duration_s "
        "FROM route_stops WHERE tenant_id = ? AND plan_id = ? ORDER BY vehicle_id, seq",
        (tenant_id, row["plan_id"]),
    ).fetchall()
    plan["stops"] = [dict(s) for s in stops]
    return plan


def get_plan(tenant_id: str, plan_id: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT plan_id, plan_date, created_at, summary_json FROM route_plans WHERE tenant_id = ? AND plan_id = ?",
            (tenant_id, plan_id),
        ).fetchone()
        return _plan_row(c, tenant_id, row) if row else None


def latest_plan(tenant_id: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT plan_id, plan_date, created_at, summary_json FROM route_plans WHERE tenant_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return _plan_row(c, tenant_id, row) if row else None


def list_plans(tenant_id: str) -> list[dict]:
    """Plans without their stops, newest first."""
    with connect() as c:
        rows = c.execute(
            "SELECT plan_id, plan_date, created_at, summary_json FROM route_plans WHERE tenant_id = ? "
            "ORDER BY rowid DESC",
            (tenant_id,),
        ).fetchall()
    return [{**json.loads(r["summary_json"]), "plan_date": r["plan_date"], "created_at": r["created_at"]} for r in rows]


def planned_etas(tenant_id: str) -> dict[str, str]:
    """shipment_id -> planned arrival (company local time) in the most recent plan that contains it."""
    with connect() as c:
        rows = c.execute(
            "SELECT s.shipment_id, s.eta FROM route_stops s JOIN route_plans p "
            "ON p.tenant_id = s.tenant_id AND p.plan_id = s.plan_id WHERE s.tenant_id = ? ORDER BY p.rowid",
            (tenant_id,),
        ).fetchall()
    return {r["shipment_id"]: r["eta"] for r in rows}  # a later plan overwrites an earlier one


def delivery_times(tenant_id: str) -> dict[str, str]:
    """shipment_id -> moment it was delivered (UTC)."""
    with connect() as c:
        rows = c.execute(
            "SELECT shipment_id, MAX(at) AS at FROM shipment_events WHERE tenant_id = ? AND status = 'livree' "
            "GROUP BY shipment_id",
            (tenant_id,),
        ).fetchall()
    return {r["shipment_id"]: r["at"] for r in rows}


def count_failed_deliveries(tenant_id: str) -> int:
    """Shipments that left (en_transit) and came back (retour)."""
    with connect() as c:
        return c.execute(
            "SELECT COUNT(*) FROM shipments s WHERE s.tenant_id = ? AND s.status = 'retour' AND EXISTS "
            "(SELECT 1 FROM shipment_events e WHERE e.tenant_id = s.tenant_id AND e.shipment_id = s.shipment_id "
            "AND e.status = 'en_transit')",
            (tenant_id,),
        ).fetchone()[0]


def count_shipments_of_order(tenant_id: str, order_ref: str) -> int:
    with connect() as c:
        return c.execute(
            "SELECT COUNT(*) FROM shipments WHERE tenant_id = ? AND order_ref = ?", (tenant_id, order_ref)
        ).fetchone()[0]


def list_unnotified_alerts(tenant_id: str) -> list[dict]:
    """Open alerts the core has not been told about yet."""
    with connect() as c:
        rows = c.execute(
            "SELECT order_ref, kind, message FROM alerts WHERE tenant_id = ? AND resolved_at IS NULL "
            "AND notified_at IS NULL ORDER BY created_at",
            (tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_alert_notified(tenant_id: str, order_ref: str, kind: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE alerts SET notified_at = ? WHERE tenant_id = ? AND order_ref = ? AND kind = ?",
            (now(), tenant_id, order_ref, kind),
        )


# ---------------------------------------------------------------------------
# Access codes of the web interface (only salted hashes are kept)
# ---------------------------------------------------------------------------
def set_access_code(tenant_id: str, role: str, salt: str, code_hash: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO ui_access (tenant_id, role, salt, code_hash, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, role) DO UPDATE SET salt=excluded.salt, code_hash=excluded.code_hash, "
            "created_at=excluded.created_at",
            (tenant_id, role, salt, code_hash, now()),
        )


def list_access_codes(tenant_id: str) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT role, salt, code_hash FROM ui_access WHERE tenant_id = ?", (tenant_id,))]


def get_plan_paths(tenant_id: str, plan_id: str) -> dict[str, list]:
    """vehicle_id -> road line [[lat, lon], ...] of a plan (empty when planned without the road network)."""
    with connect() as c:
        rows = c.execute(
            "SELECT vehicle_id, geometry_json FROM route_paths WHERE tenant_id = ? AND plan_id = ?", (tenant_id, plan_id)
        ).fetchall()
    return {r["vehicle_id"]: json.loads(r["geometry_json"]) for r in rows}
