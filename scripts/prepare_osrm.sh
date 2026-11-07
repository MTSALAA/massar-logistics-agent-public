#!/usr/bin/env sh
# Prepare the road network for OSRM (run once; needs Docker, a few GB of RAM for a country-size map).
#
#   sh scripts/prepare_osrm.sh                      # whole Algeria (large download)
#   sh scripts/prepare_osrm.sh <url-to-a-smaller-regional.osm.pbf>
#
# The map comes from OpenStreetMap through Geofabrik. Files go to ./osrm-data (ignored by git).
# Then start OSRM with:  docker compose --profile routing up -d   and set OSRM_URL=http://osrm:5000 in .env
set -eu
MAP_URL="${1:-https://download.geofabrik.de/africa/algeria-latest.osm.pbf}"
DATA="$(cd "$(dirname "$0")/.." && pwd)/osrm-data"
mkdir -p "$DATA"

if [ ! -f "$DATA/region.osm.pbf" ]; then
  echo "Downloading $MAP_URL ..."
  curl -L -o "$DATA/region.osm.pbf" "$MAP_URL"
fi

docker run --rm -v "$DATA:/data" osrm/osrm-backend osrm-extract -p /opt/car.lua /data/region.osm.pbf
docker run --rm -v "$DATA:/data" osrm/osrm-backend osrm-partition /data/region.osrm
docker run --rm -v "$DATA:/data" osrm/osrm-backend osrm-customize /data/region.osrm
echo "Done. Start OSRM with: docker compose --profile routing up -d"
