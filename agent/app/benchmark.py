"""Synthetic test set used to measure the optimiser (speed and distance saved).

Method (also in docs/how-it-works/iteration-3-route-optimization.md):
  - a depot and `stops` delivery points drawn uniformly (fixed seed) in a box of about 20 km around Bejaia;
  - each stop needs 1 to 4 units; vehicles are identical and large enough for the whole load;
  - distances are straight lines unless a road matrix is given, so the numbers do not depend on a map;
  - gain = (distance of the order-entry baseline - distance of the optimised plan) / baseline.
"""
import random
import time

from . import planner, routing

DEPOT = (36.7509, 5.0567)  # Bejaia
BOX_DEGREES = 0.09  # about 10 km each way


def make_instance(stops: int, seed: int = 42, vehicles: int = 4, capacity: int = 40):
    # Reproducible synthetic test data, not security: a seeded generator is exactly what is wanted here.
    rng = random.Random(seed)  # nosec B311
    points = [DEPOT] + [
        (DEPOT[0] + rng.uniform(-BOX_DEGREES, BOX_DEGREES), DEPOT[1] + rng.uniform(-BOX_DEGREES, BOX_DEGREES))
        for _ in range(stops)
    ]
    demands = [0] + [rng.randint(1, 4) for _ in range(stops)]
    return points, demands, [capacity] * vehicles


def run(stops: int = 50, seed: int = 42, solver_seconds: float = 5, vehicles: int = 4, capacity: int = 40) -> dict:
    points, demands, capacities = make_instance(stops, seed, vehicles, capacity)
    matrix = routing.straight_line_matrix(points)
    started = time.perf_counter()
    solved = planner.solve(matrix, demands, capacities, service_s=600, horizon_s=9 * 3600, solver_seconds=solver_seconds)
    elapsed = time.perf_counter() - started
    served = sorted(node for route in solved["routes"] for node in route)
    optimised = sum(planner.route_distance(matrix, r) for r in solved["routes"])
    baseline = planner.baseline_distance(matrix, served, demands, capacities)
    return {
        "stops": stops, "seed": seed, "vehicles": vehicles, "served": len(served), "dropped": len(solved["dropped"]),
        "solver_seconds": solver_seconds, "elapsed_seconds": round(elapsed, 2),
        "baseline_km": round(baseline / 1000, 1), "optimised_km": round(optimised / 1000, 1),
        "gain_pct": round((baseline - optimised) / baseline * 100, 1) if baseline else 0.0,
    }
