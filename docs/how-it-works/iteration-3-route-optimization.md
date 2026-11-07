# Route optimization (version 0.4.0)

## What it does

Given the shipments waiting in `preparation`, the agent builds the delivery routes of the day: which vehicle takes which stops, in which order, and when it should arrive. It minimises the distance driven while respecting each vehicle's capacity and the working hours, and it measures how much distance it saves against an unoptimised sequence.

```
 shipments in preparation
        |  1. locate      text address -> coordinates (cache, then Nominatim) or coordinates given/entered
        v
 depot + stops (lat, lon)
        |  2. measure     distance and duration between every pair of points (OSRM, or straight lines)
        v
 matrix
        |  3. solve       OR-Tools: capacity, working hours, stop duration, minimum distance
        v
 routes + arrival times
        |  4. compare     baseline = same stops in order of entry  ->  gain %
        v
 stored plan (P1, P2, ...), alerts for stops left out
```

## 1. Locating shipments (`geo.py`, `planning.locate_shipments`)

A shipment needs coordinates to be planned.

- Coordinates sent with the order (`latitude`/`longitude`) are used as they are.
- A text address is looked up in the company's **cache** first (`geocache` table; the key ignores case and extra spaces). Only on a miss does the agent ask **Nominatim** (OpenStreetMap's search service).
- Nominatim policy is respected: at most **one request per second** (a lock and a 1.1 s spacing), an identifying `User-Agent` (`MassarLogisticsAgent/0.4 (<NOMINATIM_CONTACT>)`), and **caching of answers**. An answer "address unknown" is cached; a failure (network, HTTP error, garbled answer) is not, so it is retried next time.
- Lookups run **in the background after the reply** to the core when an order arrives, so the plan usually finds the coordinates already cached. The plan also locates anything still missing.
- **Fallbacks:** if the address is unknown the order gets the alert `address_not_found`; if geocoding is off or down, `address_not_located`. Either way the coordinates can be entered by hand with `PUT /tenant/{id}/shipments/{id}/location` (which closes the alert).
- The cache is per company: one company never reads another's lookups, and deactivation deletes it.

Geocoding is **off unless `NOMINATIM_URL` is set** (the Docker setup turns it on with the public service; set it empty to switch off).

## 2. The distance matrix (`routing.py`)

- With **OSRM** (`OSRM_URL`), one call to its `table` service gives the real road distance (metres) and driving time (seconds) between all points (coordinates are sent as `lon,lat`). Pairs with no road connection are treated as very far. OSRM also says how far each point is from the road it attached it to; a point more than 500 m away is reported as a suspicious location (see the 1.1.0 notes).
- **Fallback:** without `OSRM_URL`, or if OSRM does not answer or answers badly, the matrix is built from **straight-line (haversine) distances multiplied by 1.3** (a road is longer than the crow flies; added in 1.1.0) with an assumed 35 km/h average speed. The plan states which was used: `source: "osrm"` or `"straight_line"`, with a `source_note`. Straight lines under-estimate real distances, but the comparison with the baseline stays fair since both use the same matrix.

To run OSRM on a real map (one time, large download, a few GB of RAM for a country):

```powershell
.\scripts\prepare_osrm.ps1                 # or: sh scripts/prepare_osrm.sh
docker compose --profile routing up -d      # starts OSRM on port 5000
# in .env:  OSRM_URL=http://osrm:5000
```

The script downloads the OpenStreetMap extract of Algeria from Geofabrik (a smaller regional `.osm.pbf` URL can be passed instead) and prepares it. The map files go to `osrm-data/`, which git ignores.

## 3. The solver (`planner.py`)

A *capacitated vehicle routing problem with working hours*, solved with Google OR-Tools:

- Point 0 is the depot; every vehicle starts and ends there.
- Each stop has a **demand**: the sum of its order's line quantities, rounded up, at least 1. A vehicle's load cannot exceed its **capacity** (the configuration's `vehicles`).
- Serving a stop takes `stop_duration_minutes`. A vehicle must be **back at the depot before the end** of the working hours (`working_hours`).
- The cost minimised is the **total distance**.
- A stop that cannot fit anywhere is **dropped with a large penalty** rather than making the whole problem unsolvable. Stops that could never be served are named beforehand: `exceeds_capacity` (bigger than every vehicle) and `too_far_for_working_hours` (the round trip alone is longer than the day). Anything else dropped by the solver is `no_room`. Every left-out stop raises the alert `unplanned` with its reason, and is listed in the plan's `unassigned`.
- Search: first solution by *path cheapest arc*, improved by *guided local search* until the **time limit** (`solver_seconds`, default 5, at most 30). The solver uses the time it is given, so a plan takes about that long.
- An order can ask for a delivery day with `date_livraison_souhaitee` (ISO date). A plan for an earlier day lists it under `deferred` instead of planning it.

## 4. The gain (`planner.baseline_*`, `benchmark.py`)

The **baseline** is the "order-entry sequence": the stops in the order they were entered, vehicles filled one after the other (the next vehicle is used when the current one is full; the last one takes the rest). No optimisation. The plan reports both distances and `gain_pct = (baseline - optimised) / baseline`. The baseline ignores working hours and may overload the last vehicle, so that it covers the same stops as the optimised plan.

**Measured** on the synthetic test set (`python scripts/benchmark_routing.py`): 50 stops drawn uniformly at random (fixed seeds 1 to 5) in a box of about 20 km around Bejaia, 4 vehicles of capacity 40, straight-line distances, solver limit 5 s, Python 3.12 on a Windows laptop:

| Result | Value |
|--------|-------|
| Time for a 50-stop plan | 5.0 s (the solver time limit; target: under 10 s) |
| Distance saved against the baseline | 74.5 % to 76.3 %, mean 75.4 % (target: at least 15 %) |
| Stops served | 50 of 50 in every run |

Read the gain with care: random addresses entered in random order are the **worst case for the baseline**, so 75 % is high. Real orders are partly grouped by area, which makes a real baseline shorter and the real gain smaller. The method is fixed and repeatable, which is what matters for the follow-up documents; re-measure on real data when it exists.

## 5. The plan and the API

A plan is stored with its stops (`route_plans`, `route_stops`) and returned like this (shortened):

```json
{ "plan_id": "P1", "plan_date": "2026-11-08", "source": "straight_line",
  "total_distance_m": 133900, "baseline_distance_m": 545000, "gain_pct": 75.4,
  "stops_planned": 50, "vehicles_used": 4,
  "routes": [ { "vehicle_id": "V1", "stops": 13, "load": 38, "capacity": 40,
                "distance_m": 31200, "duration_s": 27100, "back_at_depot": "2026-11-08T15:31:40" } ],
  "stops": [ { "vehicle_id": "V1", "seq": 1, "shipment_id": "EXP-CMD-7", "address": "...",
               "lat": 36.77, "lon": 5.05, "demand": 2, "eta": "2026-11-08T08:12:00",
               "leg_distance_m": 2400, "leg_duration_s": 247 } ],
  "unassigned": [], "deferred": [] }
```

Arrival times (`eta`) are the company's local clock times (working hours start the day); they carry no time zone.

| Route (under `/tenant/{id}`) | Purpose |
|------------------------------|---------|
| `POST /plans` | Plan now. Body (optional): `{"plan_date": "2026-11-08", "solver_seconds": 5}` |
| `GET /plans`, `GET /plans/latest`, `GET /plans/{plan_id}` | Read plans (newest first / latest / one) |
| `PUT /shipments/{id}/location` | `{"lat": .., "lon": ..}` for a shipment in preparation |

A plan covers every shipment in `preparation` (those already `en_transit` are not planned again). Planning again creates a new plan; the latest is the current one. Planning needs a `depot` in the company configuration (422 otherwise).

## Outside calls (for the execution report)

| Service | When | What leaves the agent |
|---------|------|-----------------------|
| Platform core (`CORE_URL`) | Publishing shipments, pulling orders and stock | Shipment status; the company's key |
| Nominatim (`NOMINATIM_URL`, public service) | A text address with no cached coordinates | The address text and the country code |
| OSRM (`OSRM_URL`, own container) | Each plan, if OSRM is set | Coordinates only, to the company's own machine |

## Files

`geo.py`, `routing.py`, `planner.py`, `planning.py`, `benchmark.py`, `ops_routes.py` (plan routes), `scripts/prepare_osrm.*`, `scripts/benchmark_routing.py`, `docker-compose.yml` (`osrm` service, `routing` profile).

## Tests

`tests/test_geo_routing.py` (haversine, Nominatim: cache, politeness, failures; OSRM: format, fallbacks), `tests/test_planner.py` (solver on small cases, capacity, working hours, the 50-stop benchmark against the targets), `tests/test_planning.py` (stored plans, unserved stops, deferred orders, geocoding in the flow, isolation, the API).

## Limits

- **Not yet run against the real Nominatim and OSRM.** Both are tested against simulated services that follow their documented formats. The first real run needs internet (Nominatim) and the prepared map (OSRM).
- The map download is not done by the project's setup: run the prepare script yourself.
- One daily time window per company; no per-stop time windows yet, and a single plan date at a time.
- Demand is counted in units from the order lines; weights and volumes are not modelled.
- Straight-line mode under-estimates distances and durations.
