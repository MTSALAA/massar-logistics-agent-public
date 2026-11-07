# Interface and tracking (version 0.5.0)

## What it does

People can now use the agent from a browser. A **logistics manager** sees the indicators, the shipments and the alerts, and plans the routes with one click. A **driver** sees the stops in order on a phone-friendly page and reports each step: picked up, delivered, or failed (with a reason, and optionally a new day). Both can see the **route map**. In the background, the agent measures punctuality, raises alerts for delays and failures, and reports them to the core.

## Signing in (`ui_auth.py`)

The company's API key belongs to the core; the people using the interface never see it. So the interface has its own access codes:

1. Whoever holds the company key (the platform operator) generates the codes:
   `POST /tenant/{id}/access-codes` (with the key as Bearer token). The reply gives a **manager code**, a **driver code** and the sign-in address `/ui/login?tenant=<id>`. The codes are shown **only in this reply**; generating again replaces them.
2. The operator hands the codes to the right people. A code looks like `K7M2-9QXP-4WBN` (12 symbols out of 32, no 0/O/1/I, so it is easy to read out).
3. At `/ui/login` the person enters the company and the code. The code decides the role.

Protections: only **salted PBKDF2 hashes** of the codes are stored; comparison is constant-time; after **5 wrong attempts in a minute** (per company and client address) sign-in is blocked for a minute, even with the right code; the session is a cookie **signed with HMAC-SHA256**, `HttpOnly`, `SameSite=Lax`, valid 12 hours, and it stops working when the company is deactivated. The signing key is `SESSION_SECRET` (or random at start, which ends sessions on restart). Every page checks the role: drivers cannot open the dashboard or plan.

Actions (planning, status changes) are accepted only from HTMX (the `HX-Request` header). A page on another site cannot send that header, which prevents forged requests. Anything that comes from orders (addresses, names) is **escaped** when displayed, so a malicious address cannot inject script (a test checks this on every page).

The `/ui` pages are for browsers, so they answer HTML instead of the JSON envelope. The core never calls them; the contract endpoints are unchanged.

> Version 1.1.0 redesigned these pages and added the shipment page; see [Polish and reliability](iteration-6-polish-and-reliability.md) and the [User guide](../User-guide.md) for the current screens.

## The pages

| Page | Who | What |
|------|-----|------|
| `/ui/login` | everyone | Company and access code |
| `/ui` | manager | Indicators, "Plan now" button, alerts, shipments table; refreshes by itself every 15 s |
| `/ui/map` | both | Leaflet map of the latest plan: the depot, one colour per vehicle, numbered stops, popups with order, address and planned time. `?plan=P2` shows an older plan. |
| `/ui/driver` | both | The shipments still to do, in visiting order, filterable by vehicle. Buttons: "Picked up, out for delivery", "Delivered", and a "Delivery failed" form. |

Technology: server-rendered **Jinja2** templates (`agent/app/templates`), **HTMX** to update parts of a page without a page reload, **Leaflet** with OpenStreetMap tiles for the map. All three come from public CDNs in the visitor's browser (HTMX and Leaflet from unpkg, tiles from OpenStreetMap), so the visitor needs internet access for the map. Lines on the map join the stops in visiting order; they are not the road geometry.

## Failed deliveries and rescheduling

A driver can report a failure only for a shipment that is on the road. The reason is required. If a day is given, the agent creates a **new shipment** for the same order, `EXP-<order>-R1` (then `-R2`...), in `preparation` with the same address and a "not before" date. The failed shipment stays in the history as `retour`, and both are published to the core (the Billing agent sees both). The order now follows the newest shipment (if the order is cancelled later, the new shipment is cancelled). The planner leaves the new shipment out of plans for earlier days and includes it from its date.

## Indicators (`indicators.py`)

| Indicator | How it is computed |
|-----------|--------------------|
| Shipments by status | Counts of `preparation`, `en_transit`, `livree`, `retour` |
| Failed deliveries | Shipments that left (`en_transit`) and came back (`retour`); cancellations before pick-up are not counted |
| **On-time rate** | Share of delivered shipments that arrived no later than the planned arrival + 15 minutes. Only shipments that were in a plan can be measured, and the page says how many were |
| **Kilometres per route** | Average length of the routes in all stored plans |
| **Distance saved** | All plans together: (baseline km - planned km) / baseline km |
| Open alerts | Count |

Planned arrivals are the company's local clock times; delivery times are recorded in UTC. The configuration field `utc_offset_minutes` (default 60 for Algeria) converts one to the other. Available as JSON at `GET /tenant/{id}/indicators`.

## Alerts and the core

| Alert | Raised when | Closed when | Severity sent to the core |
|-------|-------------|-------------|---------------------------|
| `delivery_failed` | A shipment on the road comes back | The order is delivered | high |
| `delay` | A shipment still waiting or on the road is more than 15 minutes past its planned arrival | It is delivered | medium |
| `unplanned` | The planner left a shipment out | It is planned | medium |
| `stock_insufficient` | Not enough stock | Stock arrives or the order ships | medium |
| `stock_unknown`, `address_missing`, `address_not_found`, `address_not_located`, `order_invalid` | See iterations 2 and 3 | Fixed | low |

Delay checks run when the dashboard refreshes its indicators and when the indicators are read through the API (there is no scheduler yet).

Each open alert is sent **once** to the core's event log with `POST {CORE_URL}/webhooks/agent-events` (`{"event_type", "severity", "data": {"order_ref", "message"}}`). If the core refuses or is down, it is retried at the next change or sync. An alert that was closed and then comes back is sent again. Shipments are published first, then alerts (`core_client.after_change`).

## Operations API additions

| Route (under `/tenant/{id}`) | Purpose |
|------------------------------|---------|
| `POST /access-codes` | Generate the interface codes (shown once) |
| `GET /indicators` | The figures above |
| `POST /shipments/{id}/status` | With `"status": "retour"`, also accepts `"reschedule_date": "2026-11-09"` |

## Data

New table `ui_access` (hashed codes). New columns: `shipments.earliest_date` and `alerts.notified_at`. Databases made by older versions are **upgraded in place** the first time they are opened (a test checks this), so no data is lost when the agent is updated. `ui_access` is in `TENANT_TABLES`, so deactivation deletes the codes too.

## Try it

```bash
docker compose up --build -d
# 1. generate the codes (key = the company's tenant_api_key from the core)
curl -X POST -H "X-Tenant-Id: <company-id>" -H "Authorization: Bearer <key>" http://localhost:9000/tenant/<company-id>/access-codes
# 2. open the sign-in address from the reply
#    http://localhost:9000/ui/login?tenant=<company-id>
```

Sign in as manager, press "Plan now" (the company needs a `depot` in its configuration and shipments in preparation), open the map. Sign in as driver in another browser (or private window) to follow a delivery.

## Tests

`tests/test_interface.py` (codes, sessions, throttling, roles, forged requests, escaping, dashboard, map, driver actions, isolation) and `tests/test_tracking.py` (indicators, delay and failure alerts, rescheduling, the alert webhook, the database upgrade).

## Limits

- Access codes must be handed out by the operator; there is no self-service.
- No scheduler: delay alerts appear when someone loads the dashboard or the indicators.
- The driver page does not geolocate the driver or show turn-by-turn directions.
- The pages were checked with automated tests and by hand in one browser; they have not been tested on many devices.
- The map and the page scripts need internet access in the browser (CDNs and map tiles).
