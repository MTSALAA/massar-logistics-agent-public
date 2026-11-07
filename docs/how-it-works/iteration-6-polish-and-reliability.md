# Polish and reliability (version 1.1.0)

## What it does

No new business feature: this version makes the agent **easier to use, more honest about its data, and more robust**. It came from a review of the running application, which found real defects. Each one is listed with its cause and its fix, because the reasons matter as much as the fixes.

## What was found and fixed

| What was seen | Cause | Fix |
|---------------|-------|-----|
| **Map pins in the sea** | The demo's coordinates had been typed by hand and were wrong. The map draws exactly what it is given. | Demo data now comes from real geocoding of known places. The agent can detect such points when the road network is connected (below), and the manager can correct a location from the shipment page. |
| **Geocoding was slow and asked Nominatim the same question many times** | Every incoming order started its own background lookup. Ten orders at once meant ten lookups of the same addresses, one second apart each: slow, and not polite to a free public service. | A lookup now checks the cache again once it holds the lock and **saves its answer before releasing it**; lookups for one company also run one after another. A test with ten simultaneous requests for one address checks that exactly **one** request leaves the agent (the first version of the fix still sent three: the test caught it). |
| **The status buttons on the shipments list did nothing** | The buttons fired an event the list was not listening for. Search worked, the buttons did not. | The list now listens for the right event. Checked in a browser: 4 delivered, 1 on the road, 1 returned, 10 to prepare, out of 16. |
| **The map slid over the sticky header, and map and stop list stayed side by side on a phone** | The map's stacking order beat the header's, and an inline style overrode the responsive layout. | The header is above the map; the layout is defined in the stylesheet and stacks on small screens. |
| **Changed styles did not reach browsers** | The browser kept an old copy of the stylesheet. | The stylesheet address carries the release and the file's modification time, so a changed file is always fetched. |
| **Everyone was signed out whenever the agent restarted or was updated** | The session signing key was random at each start. | A random key is created once and kept in the data volume (`session.key`); `SESSION_SECRET` still overrides it. A read-only volume falls back to a per-run key instead of failing. |
| **Long shipment numbers wrapped letter by letter on a narrow screen** | The table was squeezed to the screen width. | The table keeps a minimum width and scrolls sideways. |
| **Errors inside the interface showed raw JSON** | The agent's error format is meant for programs. | Pages under `/ui` show a readable error page (and a small message for HTMX fragments). Contract and operations routes keep the JSON envelope. |
| **Browsers asked for `/favicon.ico` and got an error each time** | There was no icon. | An icon and a shortcut for the old address. |
| **Signing in with the company name failed with a vague message** | The interface needs the company *identifier*, which looks like a long code. | The sign-in page says so, with an example, and the error message repeats it. |

## New behaviour

**Detour factor.** Without the road network, distances are straight lines multiplied by **1.3**, because roads are longer than the crow flies. Routes and arrival times are more realistic. The distance saved is unchanged, since both the plan and its baseline are scaled equally.

**Suspicious locations.** When OSRM is connected it reports how far each point is from the road it attached it to. A point more than **500 m** from any road (the sea, a field, a wrong address) is listed in the plan under `warnings`, gets the alert `location_suspect`, and appears with a red ring on the map. It is still planned (OSRM attaches it to the nearest road), so the day is not blocked; someone is asked to check. Without OSRM this cannot be known, and the plan does not pretend otherwise.

**Readable plan results.** A shipment left out of a plan is explained in words ("its load is bigger than every vehicle", "no vehicle had enough capacity or time left") instead of a code, both on the page and in the alert.

## Interface changes

- A real stylesheet (`app/static/app.css`) with a consistent look: colour-coded status badges, cards, a sticky header with the current page highlighted, vehicle load bars, and a phone layout with large buttons.
- **Dashboard**: indicator cards coloured by meaning; a *Vehicles in the latest plan* card with how full each van is; alerts with a title, advice and a link to the shipment; shipments list with **status filters and search**, open work first.
- **Shipment page** (new): details, timeline of events, links to the other shipments of the same order, actions, a small map, and **Set the location by hand**.
- **Map**: a vehicle toggle, a clickable stop list (so overlapping pins are never a problem), a plan selector, and red rings for suspicious points.
- **Driver page**: large buttons, a count of stops left, links to details and to the point in OpenStreetMap, a confirmation before reporting a failure.
- **Sign-in**: explanation of the company identifier and where to get it.

## Files

`app/static/app.css`, `app/static/favicon.svg`, `app/templates/*` (rewritten, plus `shipment.html`, `_shipment_detail.html`, `_plan_summary.html`, `message.html`), `app/ui_routes.py`, `app/ui_auth.py`, `app/routing.py`, `app/planning.py`, `app/geo.py`, `app/version.py`.

## Tests

`tests/test_polish.py` covers: the detour factor; the road distance (snap) reading; flagging, alerting and clearing a suspicious point; readable reasons; geocoding under load (one request per address); static files and the cache-busting address; the sign-in help; readable errors; isolation of shipment pages; the shipment page and its actions; the location form's validation; list filtering and search; alert advice; the plan card; the map's plan selector and warnings; the driver page; the persistent session key; one version number everywhere.

## Limits

- The suspicious-location check needs OSRM, which still has to be connected with a prepared map.
- The interface was checked by automated tests and by hand on a desktop-size and a phone-size view in one browser.
- The pages still load HTMX and Leaflet from a public CDN, so the browser needs internet access.
