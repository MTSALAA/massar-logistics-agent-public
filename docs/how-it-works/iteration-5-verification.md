# Verification and release (version 1.0.0)

## What it does

No new feature: this iteration proves that the agent follows the instructor's contract, finds and closes the gaps in the tests, and produces the documents needed to submit it. The result is version 1.0.0.

## The checklist and where each point is tested

The instructor's developer guide ends with a checklist. `agent/tests/test_final.py` turns each point into automated tests, so it can be re-run after every change.

| Checklist point | Tests | What they do |
|-----------------|-------|--------------|
| Mandatory endpoints answer with the envelope | `TestEnvelope` | Walks **every route** the core or an operator can call (found from the application itself, so new routes are included automatically) with no headers, a wrong key and an unknown company; also unknown routes, wrong methods, malformed bodies (not JSON, a list, empty, bad bytes) and an internal failure, which must answer 500 in the envelope without leaking details |
| Manifest valid | `TestManifest` | Fields of the contract example, `filiere`, version format, no placeholder email, manifest = code (version, `consumes`, endpoint paths), declared port = Dockerfile port, every dependency pinned, no secret written in the code |
| Isolation between companies | `TestIsolation` | Runs a complete scenario for two companies (configuration, stock, orders, alert, plan, delivery, failed delivery, access codes), then checks every list route, the interface pages and the database file itself; one company's key opens nothing of the other |
| Deactivation deletes the data | `TestPurge` | Finds the tables **in the database file** (not in the code's list), requires data in every one before, and 0 rows for the deactivated company and an unchanged count for the other after; a second test fails if a table with `tenant_id` is missing from `TENANT_TABLES`; a returning company starts from nothing with a new key |
| `/exchange/notify` with a test object | `test_contract.py`, `test_logistics.py` | Accepted, ignored when not consumed, unknown fields ignored, stale version ignored, wrong key refused |
| Speed and concurrency | `TestPerformance` | 8 simultaneous notifications of the same order give one shipment; 60 simultaneous different orders are all kept; 40 notifications average under a second each; a 50-stop plan through the API takes under 10 s with at least 15 % distance saved |

The deactivation tests were checked to be able to fail: removing a table from the purge list on purpose makes two of them fail (one found that the first version of the test trusted the list it was checking, which is why it now reads the database file instead).

## Checks done by hand

On a **fresh image** (`docker build`, then `docker run` with no volume and no settings): health, config schema, activation of two companies, status, a test order and a test stock level through `/exchange/notify`, isolation (a company sees none of the other's orders; its key on the other's route gives 401), deactivation, and a count of rows left per table in the container's database file. Also checked on the instructor's core, run locally (see [Running with the platform core](../Running-with-the-platform-core.md)): submission, certification, publication, subscription, publishing shipments, alerts, cancellation.

## Running everything

```bash
cd agent
pip install -r requirements-dev.txt
pytest                      # two to three minutes; includes the 50-stop benchmark
ruff check .
python ../scripts/benchmark_routing.py      # the figures quoted in the report
```

CI (`.github/workflows/ci.yml`) runs `ruff`, `pytest` and a Docker build on every push.

## The execution report

`docs/Rapport-execution.md` (in French, for the instructor) lists how to run the agent, the environment variables, how each checklist point was verified, **every outside network call**, the stored data, the security measures, the measured performance and the known limits. The contract forbids calls to a service that is not in this report, so update the report whenever a call is added.

## What was decided

- The browser pages (`/ui`) and FastAPI's automatic documentation (`/docs`) are not in the JSON envelope. They are not contract routes and the core never calls them; the report says so. The instructor's own example agent also keeps the automatic documentation.
- A speed experiment (SQLite write-ahead log) gave no gain and was not kept.
- A cookie option was added for deployments behind HTTPS: the session cookie is `Secure` on HTTPS, or when `SESSION_COOKIE_SECURE=1`.

## Limits

See section 9 of the report. In short: real Nominatim and OSRM runs, objects published through the core itself, the interface on phones, and the first observed run of the CI.
