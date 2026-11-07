# How the agent works

Start with [How the agent works](../How-the-agent-works.md) for the overview. Then one document per iteration, each explaining what was built, how it works inside, how to try it and what its limits are. Read them in order; each one builds on the previous.

| Document | Version | Subject |
|----------|---------|---------|
| [Foundations](iteration-0-1-foundations.md) | 0.1.0, 0.2.0 | The integration contract, tenants, security, configuration |
| [Shipments](iteration-2-shipments.md) | 0.3.0 | Receiving orders and stock, deciding what ships, tracking, publishing |
| [Route optimization](iteration-3-route-optimization.md) | 0.4.0 | Geocoding, road distances, the OR-Tools solver, the gain measurement |
| [Interface and tracking](iteration-4-interface-and-tracking.md) | 0.5.0 | Sign-in, dashboard, map, driver page, indicators, alerts |
| [Verification and release](iteration-5-verification.md) | 1.0.0 | The instructor's checklist as tests, the execution report |
| [Polish and reliability](iteration-6-polish-and-reliability.md) | 1.1.0 | Defects found in the running app and their fixes, new pages, location checks |
| [Navigation and real roads](iteration-7-navigation-and-roads.md) | 1.2.0 | Navigate buttons, road lines from OSRM, housekeeping, the road network set-up |
| [Security review](iteration-8-security-review.md) | 1.2.1 | Audit of files, history, code and dependencies; fixes; how to publish safely |

## The whole picture

```
 Other agents / employees                     PLATFORM CORE (instructor)
 (Commercial, Stock, dashboard)               exchange bus, subscriptions, catalog
        |  publish commande, stock_level            ^   |
        v                                           |   | push  POST /exchange/notify
   [ core ] -------------------------------------->|---+--------------------+
                                                    |                        v
                                                    |             +--------------------+
                          publish expedition        |             |  Massar agent      |
                  POST /exchange/agent/expedition <-+-------------|  (this repository) |
                                                                  |                    |
   pull fallback  GET /exchange/agent/{commande, stock_level} --->|  decide -> locate  |
                                                                  |  -> plan -> track  |
                                                                  +----------+---------+
                                                                             |
                                       Nominatim (addresses)  <--------------+--> OSRM (road distances)
                                       public service, 1 req/s, cached          own container, optional
```

Rules that never change, whatever the iteration:

- Every answer, errors included, is `{"status": "ok" | "error", "data": ..., "error": ...}`.
- Every row of data belongs to one company (`tenant_id`), and deactivating a company deletes all of it.
- The agent never calls another agent; it only talks to the core.
- Anything the core sends that the agent does not know is ignored, never an error.

## Where things are

| Path | Role |
|------|------|
| `agent/app/main.py` | The contract endpoints (`/health`, `/tenant/*`, `/config-schema`, `/exchange/notify`) |
| `agent/app/auth.py` | Reading the tenant headers and checking the API key |
| `agent/app/config.py` | Company configuration: validation, defaults, JSON Schema |
| `agent/app/db.py` | SQLite storage; the list `TENANT_TABLES` drives the purge |
| `agent/app/logistics.py` | Shipping decision and shipment life cycle |
| `agent/app/core_client.py` | Publishing to the core, pulling from the core |
| `agent/app/geo.py`, `routing.py` | Geocoding; distance matrix and road lines (OSRM or straight lines) |
| `agent/app/maintenance.py` | Housekeeping every five minutes: delay alerts, retries towards the core |
| `agent/app/planner.py`, `planning.py` | The OR-Tools solver; planning a company's routes |
| `agent/app/ops_routes.py` | Operations API (shipments, plans, alerts, indicators, codes, sync) |
| `agent/app/ui_routes.py`, `ui_auth.py`, `templates/`, `static/` | The web interface and its sign-in |
| `agent/app/indicators.py` | Indicators and delay alerts |
| `agent/tests/` | The automated tests |
| `scripts/` | Submit to the core, benchmark, prepare the road network |
