# Navigation and real roads (version 1.2.0)

## What it does

Three things a driver and a manager asked for:

1. **"Which way is best?"** Each stop on the driver's phone has a **Navigate** button, and each van a **Navigate the next stops** button. They open the phone's own maps app with directions from where the driver is.
2. **"Show me the roads."** With the road network connected, the map draws each van's route **along the real streets**, and distances and times are real.
3. **"Do not make me open the dashboard to know a delivery is late."** A housekeeping pass every five minutes raises delay alerts and sends the core anything it has not received.

## Navigation links (`ui_routes.py`)

The agent decides the **order** of the stops and which van takes them. The phone's maps app decides the **turns**, with the driver's live position.

| Button | Where | What it opens |
|--------|-------|---------------|
| **Navigate** | Each stop (driver page, shipment page, map pins) | `https://www.google.com/maps/dir/?api=1&destination=LAT,LON&travelmode=driving`. On a phone this opens the Google Maps app (or its web page) with directions from the current position. |
| **Apple Maps** | Each stop (driver page) | `https://maps.apple.com/?daddr=LAT,LON&dirflg=d` for iPhones |
| **Navigate the next N stops** | Top of the driver page, once **one vehicle** is chosen | The same link with `waypoints=`: the stops in visiting order, the last one as the destination. Google Maps takes a destination and nine stops in between, so ten stops at a time. |

Rules:

- A stop **without coordinates** has no button.
- A stop that is **far from any road** is marked **Check the location** and is **left out of the one-tap route**, so a driver is never sent to a point in the sea. It keeps its own button.
- Tapping a button sends the coordinates of the stop(s) to Google or Apple Maps, like any map link. The agent itself sends nothing.

## Real road lines (`routing.py`, `planning.py`, `map.html`)

When OSRM is connected, planning does one more call per van: OSRM's `route` service with the stops in visiting order (depot, stops, depot), asking for the full line as GeoJSON. The line is stored with the plan in a new table `route_paths` (one row per van and plan, listed in `TENANT_TABLES`, so deactivation deletes it) and marked `road_geometry: true` in the plan. `GET /tenant/{id}/plans/{plan_id}/paths` returns it.

The map draws that line instead of straight segments, and the page says which it is: *Lines follow the real roads* or *not the roads (the road network is not connected)*. If OSRM fails for the road line, the plan is still made; only the line is missing and the map falls back to straight segments.

OSRM's default limit is **100 points per distance table**, so a plan with more than 99 stops would have silently fallen back to estimates. The compose file now starts OSRM with `--max-table-size 1000`.

## Housekeeping (`maintenance.py`)

A background thread, started with the application and stopped with it, runs every `MAINTENANCE_SECONDS` (default 300, `0` = off). For each company it:

1. raises *running late* alerts (before, they only appeared when someone opened the dashboard);
2. sends the core every shipment that failed to publish and every alert it has not received (before, a missed shipment waited for the next event).

One company failing never stops the others, and an error never stops the loop. Details of failures are not logged beyond the error type.

## Setting up the road network (measured on this project)

```powershell
.\scripts\prepare_osrm.ps1                         # download 301 MB, then extract: about 10 minutes
$env:OSRM_URL = "http://osrm:5000"
$env:OSRM_PORT = "56050"                           # only if port 5000 is taken on your machine
docker compose --profile routing up -d --build
```

| Measure | Value |
|---------|-------|
| Map (Algeria, Geofabrik) | 301 MB download |
| Preparation | extract 7.6 min, partition 55 s, customize 27 s (about 9 minutes); at least 1.2 GB of memory was in use during the extraction |
| Prepared files | 1.9 GB in `osrm-data/` (ignored by git) |
| Memory of the running service | 858 MB (the whole country fits easily in a 7 GB Docker allowance) |

The preparation script now checks Docker's exit codes: the first run of the previous version failed silently when the image could not be pulled. It also skips the work if the network is already prepared.

### First real results

| Check | Result |
|-------|--------|
| Béjaïa airport to the town centre | 9.95 km, 9.5 minutes by road, against 4.5 km in a straight line: here the road is **2.2 times** longer. The 1.3 factor used without OSRM is only an average assumption; real ratios vary a lot with the terrain, which is why the real road network matters. |
| A point in the Bay of Béjaïa | 3,408 m from the nearest road: flagged, alerted, ringed on the map |
| A point in the town centre | 21 m from the nearest road: not flagged |
| A 9-stop plan for two vans | planned in 4.2 s with real distances: 22.4 km against 35.7 km for the order-entry sequence, a **37.2 %** saving (the synthetic benchmark's 75 % uses a deliberately naive baseline) |
| Road lines | 1,051 and 252 points for the two vans, drawn along the streets |

## Bugs found while doing this

- **The route lines had not been drawn since 1.1.0.** The redesigned map added the vehicle layers before giving the map a view, and Leaflet throws on that; the pins still appeared, which hid it. The map now has a view first, and a test checks the order.
- **One wrong point shrank the whole map** (a point in the sea made the map zoom out to the whole bay). The map now frames the good stops.
- **Orange buttons were navy** on links (the style only existed for `<button>` elements).

## Tests

`tests/test_navigation.py` (link formats, the ten-stop limit, buttons per stop and for a whole route, one vehicle required, doubtful stops flagged and excluded, shipment and map pages, the road line request and its failures, plans with and without road lines, isolation and deletion of road lines, the view-before-lines order, the compose limit) and `tests/test_maintenance.py` (delay alerts and sending without anyone opening a page, one failing company, no duplicates, the loop's start, repeat, failure and stop). `tests/test_final.py` now also puts a road line into the deactivation scenario.

## Limits

- The road lines and real distances need OSRM running with a prepared map; without it everything still works with estimates.
- The agent does not track the driver or give turn-by-turn directions: the maps app does.
- Navigation uses Google Maps (and Apple Maps) because they are on nearly every phone; they are third-party services reached by the driver's own phone.
