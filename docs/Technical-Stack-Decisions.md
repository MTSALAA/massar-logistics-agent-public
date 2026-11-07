# Technical stack – Team Massar Logistics / Delivery Agent

Decided: 2026-10-21. These are the tools we use when we build. Constraint from the assignment: free / open-source tools only.
**If the contract documents (AGENT_INTEGRATION_CONTRACT, DEVELOPER_INSTRUCTIONS) impose something different, the contract wins and this file gets updated.**

| Area | Choice | Why |
|------|--------|-----|
| Language | Python 3.12 | Named in the project sheet; best ecosystem for OR-Tools and data work |
| Web framework | FastAPI + Uvicorn | Builds REST APIs fast, auto-generates OpenAPI docs (helps with the integration contract and conformity tests), async |
| Data validation | Pydantic v2 | Built into FastAPI; validates the order / stock_level / shipment objects |
| Database | SQLite (file in a Docker volume) | Revised after reading the contract: the agent must ship as one autonomous Dockerfile, and the example agent says SQLite is enough. Every table has a `tenant_id` column for isolation |
| ORM / migrations | SQLAlchemy 2 (same version as the core) | Same library as the core; keeps the option to switch to PostgreSQL later |
| Routing engine | OSRM (official Docker image) with the Algeria OpenStreetMap extract from Geofabrik | Named in the sheet; fast distance/duration matrices on the real road network |
| Route optimization | Google OR-Tools (Vehicle Routing Problem, capacities, time windows) | Free, solves the NP-hard routing problem; fits the "< 10 s for 50 stops" target |
| Geocoding | Nominatim (OpenStreetMap), with caching in the database | Free; cache avoids hitting usage limits. Fallback: coordinates entered manually |
| Map / interface | Server-rendered pages (Jinja2) + HTMX + Leaflet.js with OpenStreetMap tiles | No separate JavaScript front-end to build; one container, simple for the route map and driver page |
| HTTP client | httpx | Async calls to OSRM, Nominatim and the core |
| Tests | pytest + pytest-cov + httpx test client | Automated tests, including the "no data leak between companies" test |
| Code quality | Ruff (lint + format) | One fast tool for both |
| Packaging | Docker + Docker Compose (agent + PostgreSQL + OSRM) | Required: the agent is delivered as a Docker image |
| CI | GitHub Actions (lint, tests, Docker build on every push) | Free, integrated with the private GitHub repo |
| Versioning | Git, SemVer (0.1.0 → 1.0.0), Conventional Commits | Assessed by the instructor; matches our 6 iterations |
| Task tracking | GitHub Projects (Kanban) + Issues | Matches the hybrid method (agile for code) |
| Dev machine | Windows 11 + Docker Desktop (WSL2), VS Code | What is already available |

## Rules taken from the contract documents (verified 2026-10-21)
- Start from the instructor's `agent-exemple` (Python 3.12, FastAPI, port 9000, `python:3.12-slim` Dockerfile). Python is mandatory.
- Mandatory endpoints: `/health`, `/tenant/activate`, `/tenant/deactivate`, `/tenant/{tenant_id}/status`, `/config-schema`. Because we consume objects we also need `/exchange/notify`.
- Every response uses the envelope `{ "status": "ok|error", "data": ..., "error": ... }`, or the subscription is suspended.
- Every call carries `X-Tenant-Id` and `Authorization: Bearer <tenant_api_key>`. Filter all data by tenant.
- Manifest `agent.manifest.json`: `filiere` = `automatisation_metier`, `consumes` = `["commande", "stock_level"]`, `produces` = `["expedition"]`.
- Objects: `expedition` has `numero_expedition`, `reference_commande`, `transporteur`, `numero_suivi`, `statut` (`preparation`, `en_transit`, `livree`, `retour`). Unknown fields must be ignored silently; dates are ISO 8601 UTC.
- Never call another agent directly; publish with `POST /exchange/agent/expedition` on the core.
- No hard-coded secrets (environment variables); `/tenant/deactivate` must really delete the tenant's data.
- Any outside network call (OSRM, Nominatim) must be listed in the execution report, so keep the list short and documented.

## Notes
- OSRM runs as a separate container in Docker Compose for development. The agent image itself stays autonomous: if OSRM is unreachable it falls back to straight-line (haversine) distances and reports it in the response.
- Project layout will be decided at Iteration 0 (repository, Docker, agent skeleton).
- Authentication and security details depend on the integration contract. Nothing is assumed until we read it.
- OSRM with the whole Algeria map needs a few GB of RAM to prepare the data. We can restrict to one region for the demo if needed.
