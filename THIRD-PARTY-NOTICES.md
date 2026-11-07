# Third-party notices

The project's own code is under the [MIT licence](LICENSE). It uses the following third-party software and data, which keep their own licences. None of it is copied into this repository except where stated.

## Python packages (installed when the image is built)

| Package | Used for | Licence |
|---------|----------|---------|
| FastAPI, Starlette | The web service | MIT / BSD-3-Clause |
| Uvicorn | The server | BSD-3-Clause |
| httpx | Calls to the platform core and to map services | BSD-3-Clause |
| Jinja2 | The web pages | BSD-3-Clause |
| pydantic | Validating configuration | MIT |
| Google OR-Tools | Route optimization | Apache-2.0 |
| protobuf, NumPy, pandas | Installed with OR-Tools | BSD-3-Clause |

Development only: pytest (MIT), Ruff (MIT), bandit (Apache-2.0), pip-audit (Apache-2.0).

## Scripts loaded by the browser

| Library | Used for | Licence |
|---------|----------|---------|
| htmx 1.9.12 | Updating parts of a page | BSD-2-Clause (0BSD for newer releases) |
| Leaflet 1.9.4 | The route maps | BSD-2-Clause |

They are loaded from a public CDN, pinned by version and cryptographic hash. They are not part of this repository.

## Map data and services

- **OpenStreetMap** data, map tiles and the **Nominatim** search service are © OpenStreetMap contributors, under the [Open Database Licence (ODbL)](https://www.openstreetmap.org/copyright). The maps shown by the agent display this attribution. Nominatim's [usage policy](https://operations.osmfoundation.org/policies/nominatim/) is respected (one request per second, an identifying User-Agent, cached answers).
- **Geofabrik** provides the OpenStreetMap extract of Algeria used to prepare the road network (OpenStreetMap licence). The prepared map files are not in this repository.
- **OSRM** (Open Source Routing Machine), used through its Docker image, is BSD-2-Clause.
- Links opened by a driver's phone to **Google Maps** or **Apple Maps** are subject to those services' own terms.

## Not included

The platform core that this agent plugs into, and the course documents that describe its contract, belong to the course instructor and are **not** part of this repository.
