# Foundations (versions 0.1.0 and 0.2.0)

## What it does

The agent is a small web service in a Docker container. The platform core knows only its address (`base_url`) and its manifest. This iteration makes the agent a **good citizen of the platform**: it answers the mandatory endpoints in the right format, keeps each company's data apart, deletes it on request, checks who is calling, and accepts a configuration per company.

## The response envelope

Every response of the agent, on every route and for every error, has exactly three fields:

```json
{ "status": "ok", "data": { "uptime_seconds": 12, "version": "0.4.0" }, "error": null }
{ "status": "error", "data": null, "error": "Invalid tenant API key" }
```

`main.py` installs three handlers (HTTP errors, request validation errors, unexpected exceptions) so that nothing can leave the agent without this shape. A response outside the envelope, or any non-2xx answer to a call made by the core, makes the core suspend the company's subscription.

## The mandatory endpoints

| Route | What happens |
|-------|--------------|
| `GET /health` | Returns the uptime and version. |
| `POST /tenant/activate` | Called by the core when a company subscribes. Validates the configuration, stores the company with its API key, replies `{activated: true}`. |
| `POST /tenant/deactivate` | Called on cancellation. Deletes every row of that company, replies `{deactivated: true, data_purged: true}`. |
| `GET /tenant/{id}/status` | `{state: "running"}`, or 404 if the company is not activated. |
| `GET /config-schema` | The JSON Schema of the configuration (generated from the model in `config.py`). |
| `POST /exchange/notify` | The core pushes a `commande` or `stock_level` here (iteration 2 gives it its logic). |

## Tenants and isolation

A *tenant* is a company. The core sends two headers on every call: `X-Tenant-Id` (the company) and `Authorization: Bearer <tenant_api_key>`.

- **Every table has a `tenant_id` column** and every query filters on it. There is no query without it.
- **The purge is driven by one list**, `TENANT_TABLES` in `db.py`. `purge_tenant()` deletes the company's rows from each table of the list. A new table must be added there, otherwise deactivation would leave data behind. A test checks every table of the list.
- **The API key is checked** (`auth.py`): the key received at activation is stored, and `/exchange/notify`, `/tenant/deactivate` and all the operations routes compare the key sent with the stored one (constant-time comparison). A wrong key, or another company's key, gives 401 and changes nothing. The key is never written to a log.
- Activation itself cannot be checked, since it is how the agent learns the key. A new activation of a known company replaces its key; this follows from the contract (the core issues a new key per subscription).

## Configuration

At activation the core may send `{"config": {...}}`. `config.py` has one model that validates it, fills the defaults, and produces the JSON Schema.

| Field | Default | Rule |
|-------|---------|------|
| `depot` | none | `{lat, lon}`, latitude -90..90, longitude -180..180. Needed to plan routes, not to receive orders. |
| `vehicles` | one vehicle `V1`, capacity 1000 | At least one; unique non-empty ids; capacity above 0. |
| `working_hours` | `08:00` to `17:00` | `HH:MM`; start before end. |
| `stop_duration_minutes` | 10 | 0 to 480. |
| `wait_for_stock` | false | A real true/false. |

Three decisions worth knowing:

- **An empty configuration is valid.** The core's web page subscribes without any configuration, so every field has a default.
- **Unknown fields are ignored**, as the data contract requires.
- **An invalid configuration is refused with 422** and a readable message, and nothing is stored (a previous valid configuration stays). The core hides the message from the company and only shows "HTTP error 422"; the explanation is in the agent's answer and logs.

To send a configuration through the core, call its API (the web page cannot): `POST /subscriptions` with `{"company_id", "agent_slug": "massar-logistique", "config": {...}}`.

## Files

`main.py`, `envelope.py`, `auth.py`, `config.py`, `db.py` (the tenants table), `agent.manifest.json`, `Dockerfile`, `.github/workflows/ci.yml`, `scripts/submit_agent.py`.

## Try it

```bash
docker compose up --build -d
curl http://localhost:9000/health
curl http://localhost:9000/config-schema
```

Activate and deactivate by hand (the key is any text you choose here):

```bash
curl -X POST http://localhost:9000/tenant/activate -H "X-Tenant-Id: A" -H "Authorization: Bearer my-key" -H "Content-Type: application/json" -d '{"config": {"wait_for_stock": true}}'
curl -X POST http://localhost:9000/tenant/deactivate -H "X-Tenant-Id: A" -H "Authorization: Bearer my-key"
```

To run it for real against the instructor's core, follow [Running the agent with the platform core](../Running-with-the-platform-core.md).

## Tests

`tests/test_contract.py` (envelope on every route and error, headers, isolation, purge, key check) and `tests/test_config.py` (defaults, validation, schema).

## Limits

- Activation cannot authenticate the caller (see above).
- The core's `X-Platform-Signature` header is a fixed placeholder, so the agent does not rely on it.
