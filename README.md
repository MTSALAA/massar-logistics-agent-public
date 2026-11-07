# Massar – Logistics / Delivery Agent

> Chaque commande en stock devient une expédition planifiée, optimisée et suivie.

An autonomous logistics agent for a multi-company business platform. It receives orders and stock levels, decides which orders can ship, plans optimized delivery routes on the real road network, tracks each shipment until delivery, and publishes the shipment object for the Billing agent.

Built by Team Massar (ESTIN, 2nd year) for the *Conduite de projet* mini-project.

## Status

Version 1.2.1: the agent activates on the core with a validated configuration, receives `commande` and `stock_level`, decides which orders can ship, tracks each shipment from `preparation` to `livree` or `retour`, publishes every `expedition` to the core, plans optimized delivery routes (geocoding with Nominatim, road distances with OSRM or straight lines, Google OR-Tools), and offers a web interface (manager dashboard, route map, driver page) with indicators and alerts. **Security:** see [SECURITY.md](SECURITY.md).

**Documentation:** [How the agent works](docs/How-the-agent-works.md) (overview with diagrams), [User guide](docs/User-guide.md), [API reference](docs/API-reference.md), and [all documents](docs/README.md).

Operations API (for the platform operator and scripts; it signs in with the company key; people use the web interface instead), all under `/tenant/{tenant_id}` with `X-Tenant-Id` and `Authorization: Bearer <key>`:

| Route | Purpose |
|-------|---------|
| `GET /shipments`, `GET /shipments/{id}` | Shipments with their timestamped history |
| `POST /shipments/{id}/status` | `{"status": "en_transit" \| "livree" \| "retour", "reason": "..."}` (a reason is required for `retour`) |
| `GET /orders` | Decision taken for each order |
| `GET /alerts` | Orders that cannot ship, with the reason |
| `POST /sync` | Read orders and stock from the core, publish what is pending |
| `POST /plans`, `GET /plans`, `GET /plans/latest`, `GET /plans/{id}` | Plan optimized routes now; read plans (vehicles, ordered stops, arrival times, distance saved) |
| `PUT /shipments/{id}/location` | Set coordinates by hand when an address cannot be located |
| `GET /indicators` | On-time rate, km per route, distance saved, failures |
| `POST /access-codes` | Create the manager and driver codes of the web interface (`/ui/login`) |

The delivery address is read from optional order fields (`docs/Adresse-livraison-proposition.md`). Set `CORE_URL` so the agent can reach the core (see `.env.example`).

| Iteration | Version | Goal |
|-----------|---------|------|
| 0 | 0.1.0 | Repository, Docker, agent skeleton |
| 1 | 0.2.0 | Mandatory endpoints and manifest; the agent activates on the core |
| 2 | 0.3.0 | Receive `commande` and `stock_level`, publish `expedition` |
| 3 | 0.4.0 | Route optimization with OSRM and OR-Tools |
| 4 | 0.5.0 | Shipment tracking, map, driver page |
| 5 | 1.0.0 | Tests, fixes, final integration |

## Stack

Python 3.12, FastAPI, SQLite, OSRM, Google OR-Tools, OpenStreetMap, Leaflet, Docker, GitHub Actions. All free and open source.

## Run

```bash
docker compose up --build        # agent on http://localhost:9000
curl http://localhost:9000/health
```

Tests (Python 3.12):

```bash
cd agent
pip install -r requirements-dev.txt
pytest
ruff check .
```

## Repository layout

```
agent/        the agent (FastAPI app, tests, Dockerfile, manifest)
docs/         stack decisions and project deliverables
docker-compose.yml
```

## Licence

MIT, see [LICENSE](LICENSE). Third-party software and map data keep their own licences: see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The platform core this agent plugs into is not part of this repository.
