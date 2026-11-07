# API reference

Everything a program can call on the agent. For people using a browser see the [User guide](User-guide.md); for the ideas behind these calls see [How the agent works](How-the-agent-works.md). The same routes are listed interactively at `/docs` on a running agent.

**Contents**

1. [Conventions](#1-conventions)
2. [Platform contract](#2-platform-contract)
3. [Operations API](#3-operations-api)
4. [Objects](#4-objects)
5. [Configuration](#5-configuration)
6. [Alerts](#6-alerts)
7. [Environment variables](#7-environment-variables)

---

## 1. Conventions

**Response envelope.** Every answer, errors included, has exactly three fields:

```json
{ "status": "ok", "data": { }, "error": null }
{ "status": "error", "data": null, "error": "Invalid tenant API key" }
```

(The browser pages under `/ui`, the stylesheet and `/docs` are not part of the API and answer HTML.)

**Headers.** The platform and the operations API identify the company with two headers:

| Header | Value |
|--------|-------|
| `X-Tenant-Id` | The company identifier |
| `Authorization` | `Bearer <the company's tenant_api_key>` |

**Status codes.**

| Code | When |
|------|------|
| 200 | Done |
| 400 | `X-Tenant-Id` missing |
| 401 | Key missing, malformed, or not the company's key |
| 403 | The company in the path is not the one in the headers |
| 404 | Company not activated, or the shipment or plan does not exist |
| 409 | The shipment cannot move that way (see the life cycle) |
| 422 | The request is invalid; `error` says why |
| 500 | Internal failure (details are never sent) |

**Dates and times.** Instants are ISO 8601 in UTC (`2026-11-07T13:28:00+00:00`). Planned arrival times are the company's local clock time without a zone (`2026-11-08T08:25:00`). Days are `YYYY-MM-DD`.

---

## 2. Platform contract

Called by the platform core. Version 1.0 of the integration contract.

### `GET /health`

No headers. `{"status":"ok","data":{"uptime_seconds":316,"version":"1.1.0"},"error":null}`

### `POST /tenant/activate`

Headers as above (the key given here is the one the agent will expect from now on).

```json
{ "config": { "depot": {"lat": 36.7451, "lon": 5.0491}, "vehicles": [{"id": "van-1", "capacity": 16}] } }
```

Reply `data`: `{"activated": true}`. The `config` may be empty or omitted fields (defaults apply, see [Configuration](#5-configuration)); an invalid one gets 422 and nothing is stored. After the reply the agent reads the company's existing orders and stock from the core.

### `POST /tenant/deactivate`

Deletes **every** row of the company. Reply `data`: `{"deactivated": true, "data_purged": true}`. Calling it for an unknown company is harmless. The key is checked when the company exists.

### `GET /tenant/{tenant_id}/status`

`data`: `{"state": "running"}`, or 404 if the company is not activated.

### `GET /config-schema`

The JSON Schema of the company settings (generated from the same model that validates them).

### `POST /exchange/notify`

The core pushes an object. Headers as above.

```json
{ "object_type": "commande", "external_id": "CMD-1", "version": 1, "payload": { } }
```

| `data` | Meaning |
|--------|---------|
| `{"processed": true, "decision": "shipped"}` | A `commande` was stored and decided (`shipped`, `blocked`, `held`, `waiting`, `closed`, `cancelled`) |
| `{"processed": true}` | A `stock_level` was stored |
| `{"processed": false, "ignored": true}` | An object type the agent does not consume |
| `{"processed": false, "stale": true}` | An older version than the one already stored |

---

## 3. Operations API

For the platform operator and scripts. All routes are under `/tenant/{tenant_id}`, need the headers above, and the path company must match `X-Tenant-Id`.

### Shipments

| Route | Purpose |
|-------|---------|
| `GET /shipments` | All shipments with history |
| `GET /shipments/{shipment_id}` | One shipment |
| `POST /shipments/{shipment_id}/status` | Move it along its life cycle |
| `PUT /shipments/{shipment_id}/location` | Set coordinates by hand |

A shipment looks like:

```json
{
  "shipment_id": "EXP-CMD-1", "order_ref": "CMD-1", "status": "preparation",
  "carrier": "Massar Logistique", "tracking_number": "MSR-1A2B3C4D",
  "address": "Place du 1er Novembre, Bejaia", "lat": 36.7524, "lon": 5.0854,
  "failure_reason": null, "created_at": "...", "updated_at": "...",
  "published": { "pending": false, "version": 2 },
  "history": [ { "status": "preparation", "at": "2026-11-07T13:28:00+00:00", "note": "Shipment created: stock available and address known" } ]
}
```

**Change a status.**

```json
{ "status": "en_transit" }
{ "status": "livree" }
{ "status": "retour", "reason": "Customer absent", "reschedule_date": "2026-11-10" }
```

| From | Allowed to |
|------|-----------|
| `preparation` | `en_transit`, `retour` |
| `en_transit` | `livree`, `retour` |
| `livree`, `retour` | nothing (final) |

`reason` is required for `retour` (422 without). With `reschedule_date`, a new shipment `EXP-CMD-1-R1` is created for that day and returned as `rescheduled_as`. A forbidden move is 409; an unknown status 422.

**Set a location.** `{"lat": 36.7515, "lon": 5.083}`: latitude -90..90, longitude -180..180 (else 422). Only for shipments in `preparation` (else 409). It closes the alerts of that order.

### Orders, alerts, indicators

| Route | Purpose |
|-------|---------|
| `GET /orders` | The decision taken for each order |
| `GET /alerts` | Open alerts; `?include_resolved=true` for all |
| `GET /indicators` | The dashboard figures |

`GET /indicators` returns, for example:

```json
{
  "shipments": { "total": 16, "preparation": 10, "en_transit": 1, "livree": 4, "retour": 1 },
  "deliveries": { "delivered": 4, "failed": 1, "measured_for_punctuality": 4, "on_time": 4, "on_time_rate_pct": 100.0 },
  "routes": { "plans": 1, "routes_planned": 2, "km_per_route": 24.6, "total_km": 49.2, "baseline_km": 96.6,
              "gain_pct": 49.0, "latest_plan": "P1", "latest_gain_pct": 49.0, "distance_source": "straight_line" },
  "open_alerts": 4
}
```

### Plans

| Route | Purpose |
|-------|---------|
| `POST /plans` | Plan now. Body (optional): `{"plan_date": "2026-11-08", "solver_seconds": 5}` |
| `GET /plans` | Plans, newest first, without their stops |
| `GET /plans/latest`, `GET /plans/{plan_id}` | One plan with its stops |
| `GET /plans/{plan_id}/paths` | The road line of each van: `{"van-1": [[lat, lon], ...]}`. Empty when the plan was made without the road network. |

`solver_seconds` is the optimiser's time budget: more than 0, at most 30, default 5. A plan covers every shipment in `preparation`. It needs a `depot` in the company settings (else 422). The reply, shortened:

```json
{
  "plan_id": "P1", "plan_date": "2026-11-08", "source": "straight_line", "source_note": "OSRM_URL not set",
  "stops_planned": 13, "vehicles_used": 2, "total_distance_m": 49200, "baseline_distance_m": 96600, "gain_pct": 49.0,
  "routes": [ { "vehicle_id": "van-1", "stops": 7, "load": 15, "capacity": 16, "distance_m": 39100,
                "duration_s": 8000, "back_at_depot": "2026-11-08T10:17:00" } ],
  "stops": [ { "vehicle_id": "van-1", "seq": 1, "shipment_id": "EXP-CMD-2012", "order_ref": "CMD-2012",
               "address": "Bir Slam Bejaia", "lat": 36.7259, "lon": 5.0571, "demand": 1,
               "eta": "2026-11-08T08:05:00", "leg_distance_m": 4300, "leg_duration_s": 460 } ],
  "unassigned": [ { "shipment_id": "EXP-CMD-2014", "order_ref": "CMD-2014", "reason": "no_location",
                    "explanation": "it has no coordinates yet" } ],
  "deferred": [], "warnings": [], "road_geometry": false
}
```

`road_geometry` is `true` when the plan used the road network and the road line of each van was stored (read it with `GET /plans/{plan_id}/paths`).

`unassigned[].reason` is `no_location`, `exceeds_capacity`, `too_far_for_working_hours` or `no_room`. `deferred` lists shipments wanted on a later day. `warnings` lists stops more than 500 m from a road (only when OSRM is connected): `{"shipment_id", "order_ref", "snap_distance_m"}`. If nothing could be planned, `plan_id` is `null` and nothing is stored.

### Access codes and synchronisation

| Route | Purpose |
|-------|---------|
| `POST /access-codes` | Create the manager and driver codes of the web interface. They are shown **only in this reply**, and replace the previous codes. |
| `POST /sync` | Read orders and stock from the core, then publish what is pending and send open alerts |

```json
{ "company": "3f8c2a10-...", "sign_in": "/ui/login?tenant=3f8c2a10-...",
  "codes": { "manager": "K7M2-9QXP-4WBN", "driver": "3FHT-8RZD-P2CV" } }
```

---

## 4. Objects

What the agent consumes and produces on the exchange bus (platform data contract).

**`commande`** (consumed). Fields the agent uses; any other field is ignored.

| Field | Used for |
|-------|----------|
| `statut` | `en_attente`, `confirmee`, `expediee`, `facturee`, `annulee` (missing means confirmed) |
| `lignes` | List of `{sku, quantite}`: required, quantities above 0 |
| `adresse_livraison` | Text, or an object with `rue`, `adresse`, `ligne1`, `ligne2`, `code_postal`, `ville`, `wilaya`, `pays` (and optionally `latitude`/`longitude`) |
| `client.adresse`, `client.nom` | Address fallback; the client's name on pages |
| `latitude`, `longitude` | Coordinates of the delivery (skip geocoding) |
| `date_livraison_souhaitee` | Earliest day to deliver (ISO date) |

**`stock_level`** (consumed): `sku` and `quantite_disponible`. Several objects with the same `sku` (one per warehouse) are added together.

**`expedition`** (produced), published with `POST {core}/exchange/agent/expedition`:

```json
{ "external_id": "EXP-CMD-1",
  "payload": { "numero_expedition": "EXP-CMD-1", "reference_commande": "CMD-1",
               "transporteur": "Massar Logistique", "numero_suivi": "MSR-1A2B3C4D", "statut": "en_transit" } }
```

`statut` is `preparation`, `en_transit`, `livree` or `retour`. The identifier stays the same on every update so the core raises the version of one object.

---

## 5. Configuration

Sent at activation, validated and completed with defaults. Unknown fields are ignored.

| Field | Type | Default | Rule |
|-------|------|---------|------|
| `depot` | `{lat, lon}` | none | Latitude -90..90, longitude -180..180. Required to plan. |
| `vehicles` | list of `{id, capacity}` | one `V1`, capacity 1000 | 1 to 100; unique non-empty ids; capacity above 0 |
| `working_hours` | `{start, end}` | `08:00`, `17:00` | `HH:MM`, start before end |
| `stop_duration_minutes` | number | 10 | 0 to 480 |
| `utc_offset_minutes` | integer | 60 | -720 to 840 |
| `wait_for_stock` | boolean | false | a real true/false |

---

## 6. Alerts

Open alerts appear in `GET /alerts`, on the dashboard, and in the core's event log with these severities.

| `kind` | Severity | Raised when | Closed when |
|--------|----------|-------------|-------------|
| `address_missing` | low | A confirmed order has no address or coordinates | A new version of the order has one |
| `address_not_found` | low | The geocoder does not know the address | The location is set or the address fixed |
| `address_not_located` | low | Geocoding is off or down | The location is set |
| `stock_insufficient` | medium | Not enough stock for a line | Stock arrives, or the order ships |
| `stock_unknown` | low | No stock level for a product | A stock level arrives |
| `order_invalid` | low | No valid order line | The order is fixed |
| `unplanned` | medium | The planner left the shipment out | It is planned |
| `location_suspect` | low | The point is more than 500 m from any road | The location is corrected |
| `delay` | medium | Past the planned arrival by more than 15 minutes | It is delivered |
| `delivery_failed` | high | A shipment on the road came back | The order is delivered |

---

## 7. Environment variables

All optional.

| Variable | Meaning | Default |
|----------|---------|---------|
| `DATABASE_PATH` | SQLite file | `/data/agent.db` |
| `CORE_URL` | Address of the platform core as seen from the agent | none (the agent then works offline and publishes later); `docker-compose.yml` sets `http://host.docker.internal:8000` |
| `NOMINATIM_URL` | Address lookup service; empty switches it off | off unless set; `docker-compose.yml` sets the public service |
| `NOMINATIM_CONTACT` | Contact shown in the User-Agent sent to Nominatim (its policy asks for one) | empty |
| `NOMINATIM_COUNTRY` | Country code to restrict lookups (`dz`) | `dz` in `docker-compose.yml` |
| `OSRM_URL` | Road-network service, for example `http://osrm:5000`; empty uses straight lines | empty |
| `MAINTENANCE_SECONDS` | Seconds between housekeeping passes (delay alerts, retries towards the core); `0` switches it off | `300` |
| `OSRM_PORT` | Host port on which the optional routing container is published, for trying it by hand (the agent reaches it over the Docker network) | `5000` |
| `SESSION_SECRET` | Key that signs sign-in cookies | a random key kept in the data volume (`session.key`) |
| `SESSION_COOKIE_SECURE` | Set to `1` behind an HTTPS proxy so the cookie is marked `Secure` | off (automatic on HTTPS) |
