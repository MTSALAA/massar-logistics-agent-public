# Running the agent with the platform core

The agent is built to plug into a **platform core** (authentication, companies, an agent catalogue and an exchange bus between agents). The core is provided by the course instructor and is not part of this repository. This page explains how to run both on one computer and what to expect. It was written from a real run on Windows 11 with Docker Desktop.

> Use test values only. Everything here is for a local test setup, never for production.

## 1. Start the core's database

The core uses PostgreSQL. Choose your own test user and password:

```bash
docker run -d --name core-db -e POSTGRES_USER=<user> -e POSTGRES_PASSWORD=<password> \
  -e POSTGRES_DB=plateforme_agents -p 55432:5432 postgres:16
```

Port `55432` is used here because `5432` is often taken. Any free port works.

## 2. Start the core

Follow the core's own instructions. In short, in a Python **3.12** virtual environment (the core pins a database driver that has no wheel for Python 3.13):

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg2://<user>:<password>@localhost:55432/plateforme_agents"
export JWT_SECRET_KEY="<a long random value>"
uvicorn app.main:app --port 8001
```

(PowerShell: `$env:DATABASE_URL = "..."`.) Tables are created at start-up. Choose a free port: `8001` here, because `8000` is often used by something else. The core's pages are `/` (home), `/inscription`, `/connexion`, `/catalogue`, `/mon-compte`, `/admin`, and its API documentation is at `/docs`.

Create the administrator once with the core's `scripts/create_admin.py` (it also needs `DATABASE_URL`), then register **two** test companies at `/inscription` (the isolation checks need two).

## 3. Start the agent

From the root of this repository:

```bash
cp .env.example .env     # then set CORE_URL=http://host.docker.internal:8001
docker compose up --build -d
curl http://localhost:9000/health
```

`CORE_URL` is the core's address **as seen from inside the agent's container**. On Docker Desktop, `host.docker.internal` is the computer itself. Without a reachable core the agent keeps working and sends its shipments later.

## 4. Submit, certify, publish, subscribe

1. **Submit:** `python scripts/submit_agent.py http://localhost:8001`. The core does not accept the manifest file as it is; the script builds the format it expects. Put a real address in `developer.email` of `agent/agent.manifest.json` first.
2. **Certify and publish:** log in to the core as the administrator, open `/admin`, certify then publish the agent.
3. **Subscribe:** log in as a test company, open `/catalogue` and subscribe. The core then calls `POST /tenant/activate` on the agent. If the agent answers outside the standard envelope or with an error status, the subscription is suspended.
4. **Check:** `curl -H "X-Tenant-Id: <company-id>" http://localhost:9000/tenant/<company-id>/status`.

The manifest's `base_url` (`http://localhost:9000`) works when the core runs on the same computer and the agent publishes port 9000. In another setup, set it to the address the core can reach. A slug can only be submitted once per core database: use a new slug or reset the core's database while developing.

## 5. Try the exchange of objects

- **Orders and stock:** publish a `stock_level` then a `commande` from the core's `/docs` (`POST /companies/{company_id}/exchange/stock_level`, then `.../commande`), authenticated as a company user. The core pushes them to the agent, which creates the shipment and publishes an `expedition` back.
- **Read back:** `GET /exchange/agent/{type}` on the core with the company's `Authorization: Bearer <key>` and `X-Tenant-Id` headers.
- **Cancel a subscription** (to see the agent delete the company's data): the core's pages have no cancel button, but its API has `POST /subscriptions/{subscription_id}/cancel`.
- **Subscribe with a configuration:** the page subscribes without one (the agent then uses its defaults). To send one, call `POST /subscriptions` with `{"company_id": "...", "agent_slug": "massar-logistique", "config": {...}}`.

## What to expect (observed)

- **What the core sends to the agent:** `X-Tenant-Id`, `Authorization: Bearer <tenant_api_key>` and a fixed `X-Platform-Signature` placeholder (not a real signature). It does not send a key on `GET /tenant/{id}/status`.
- **The core hides the agent's explanation** when it refuses something: it only reports "the agent returned HTTP error 422" and suspends the subscription. Read the agent's logs (`docker compose logs agent`) for the reason.
- **Cancelling ignores the agent's answer:** if `/tenant/deactivate` fails, the subscription is still marked cancelled on the core and the agent keeps the data.
- **Publishing a shipment** with `POST /exchange/agent/expedition` answers `{"status":"ok","data":{"id","version"}}`; the version grows each time the same `external_id` is published again.
- **Inspect the core's database:** `docker exec core-db psql -U <user> -d plateforme_agents -c "select ..."`. Useful tables: `companies`, `agents`, `agent_subscriptions`, `exchange_objects`, `agent_event_logs`.

## Starting over

`docker rm -f core-db` removes the core's database, and the next start of the core is clean. `docker compose down -v` also removes the agent's data volume.
