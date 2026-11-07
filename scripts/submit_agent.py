"""Submit the agent to the platform core (POST /agents/submit).

The core does NOT accept agent.manifest.json as is: it expects a flat JSON with
developer_email and base_url at the top level, plus the full manifest in "manifest".
This script builds that payload from agent/agent.manifest.json. Standard library only.

Usage:
    python scripts/submit_agent.py                      # core on http://localhost:8000
    python scripts/submit_agent.py http://localhost:8000
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

core_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
manifest = json.loads((Path(__file__).parent.parent / "agent" / "agent.manifest.json").read_text(encoding="utf-8"))

payload = {
    "slug": manifest["slug"],
    "name": manifest["name"],
    "version": manifest["version"],
    "description": manifest["description"],
    "filiere": manifest["filiere"],
    "category": manifest["category"],
    "developer_email": manifest["developer"]["email"],
    "team_name": manifest.get("team_name"),
    "base_url": manifest["endpoints"]["base_url"],
    "pricing_model": manifest.get("pricing_model", "flat_monthly"),
    "price": manifest.get("price", 0),
    "manifest": manifest,
    "produces": manifest.get("produces", []),
    "consumes": manifest.get("consumes", []),
}

request = urllib.request.Request(
    core_url.rstrip("/") + "/agents/submit",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request) as response:
        print(response.status, response.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode("utf-8"))
    sys.exit(1)
