"""Measure the route optimiser on the synthetic test set (speed and distance saved).

Usage (from the repo root, with the agent dependencies installed):
    python scripts/benchmark_routing.py            # 50 stops, 5 seeds, 5 s solver limit
    python scripts/benchmark_routing.py 100 3 10   # 100 stops, 3 seeds, 10 s solver limit
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from app import benchmark  # noqa: E402

stops = int(sys.argv[1]) if len(sys.argv) > 1 else 50
seeds = int(sys.argv[2]) if len(sys.argv) > 2 else 5
limit = float(sys.argv[3]) if len(sys.argv) > 3 else 5

rows = [benchmark.run(stops=stops, seed=seed, solver_seconds=limit) for seed in range(1, seeds + 1)]
for row in rows:
    print(json.dumps(row))
gains = [r["gain_pct"] for r in rows]
print(f"gain: min {min(gains)} %, mean {sum(gains) / len(gains):.1f} %, max {max(gains)} %; "
      f"slowest run {max(r['elapsed_seconds'] for r in rows)} s")
