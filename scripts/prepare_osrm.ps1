# Prepare the road network for OSRM (run once; needs Docker, about 5 GB of memory for the whole of Algeria).
#
#   .\scripts\prepare_osrm.ps1                       # whole Algeria (301 MB download, about 10 minutes, 1.9 GB on disk)
#   .\scripts\prepare_osrm.ps1 -MapUrl <url-to-a-smaller-regional.osm.pbf>
#
# The map comes from OpenStreetMap through Geofabrik. Files go to ./osrm-data (ignored by git).
# Then start OSRM with:
#   $env:OSRM_URL = "http://osrm:5000"; docker compose --profile routing up -d
# (or put OSRM_URL=http://osrm:5000 in .env). If port 5000 is taken on your machine, also set OSRM_PORT=56050:
# the agent reaches OSRM over the Docker network, the published port is only for trying it by hand.
param(
    [string]$MapUrl = "https://download.geofabrik.de/africa/algeria-latest.osm.pbf"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # the progress bar makes Invoke-WebRequest very slow

# A failing Docker command does not stop a PowerShell script by itself: check every exit code.
function Step([string]$Label, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit code $LASTEXITCODE). See the messages above." }
}

$root = Split-Path -Parent $PSScriptRoot
$data = Join-Path $root "osrm-data"
New-Item -ItemType Directory -Force -Path $data | Out-Null

if (Test-Path (Join-Path $data "region.osrm.cell_metrics")) {
    Write-Host "The road network is already prepared in $data. Delete that folder to start again."
    exit 0
}

$pbf = Join-Path $data "region.osm.pbf"
if (-not (Test-Path $pbf)) {
    Write-Host "Downloading $MapUrl ..."
    Invoke-WebRequest -Uri $MapUrl -OutFile $pbf
}

$mount = "${data}:/data"
Step "Pulling the OSRM image" { docker pull osrm/osrm-backend }
Step "osrm-extract (the longest step, several minutes)" { docker run --rm -v $mount osrm/osrm-backend osrm-extract -p /opt/car.lua /data/region.osm.pbf }
Step "osrm-partition" { docker run --rm -v $mount osrm/osrm-backend osrm-partition /data/region.osrm }
Step "osrm-customize" { docker run --rm -v $mount osrm/osrm-backend osrm-customize /data/region.osrm }
Write-Host "Done. Start OSRM with: docker compose --profile routing up -d   (and OSRM_URL=http://osrm:5000 for the agent)"
