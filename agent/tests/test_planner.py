"""The OR-Tools solver on small hand-made cases and on the 50-stop benchmark."""
import pytest

from app import benchmark, planner, routing


def line_matrix(positions):
    """Points on a straight road (metres from the depot at 0); 10 m/s, so seconds = metres / 10."""
    dist = [[abs(a - b) for b in positions] for a in positions]
    dur = [[abs(a - b) // 10 for b in positions] for a in positions]
    return routing.Matrix(dist, dur, "test")


def test_one_vehicle_visits_the_stops_in_the_shortest_order():
    matrix = line_matrix([0, 4000, 1000, 3000, 2000])  # stops entered in a zigzag order
    result = planner.solve(matrix, [0, 1, 1, 1, 1], [10], service_s=60, horizon_s=10_000, solver_seconds=1)
    assert result["dropped"] == []
    route = result["routes"][0]
    assert sorted(route) == [1, 2, 3, 4]
    assert route in ([2, 4, 3, 1], [1, 3, 4, 2])  # out along the road and back, never zigzagging
    assert planner.route_distance(matrix, route) == 8000


def test_baseline_follows_the_order_of_entry():
    matrix = line_matrix([0, 4000, 1000, 3000, 2000])
    # 0 -> 4000 -> 1000 -> 3000 -> 2000 -> 0 = 4000 + 3000 + 2000 + 1000 + 2000
    assert planner.baseline_distance(matrix, [1, 2, 3, 4], [0, 1, 1, 1, 1], [10]) == 12000


def test_baseline_fills_vehicles_one_after_the_other_and_overloads_the_last():
    routes = planner.baseline_routes([1, 2, 3, 4, 5], [0, 4, 4, 4, 4, 4], [8, 8])
    assert routes == [[1, 2], [3, 4, 5]]  # the last vehicle takes the rest even beyond its capacity


def test_capacity_is_respected_and_the_excess_is_dropped_not_crashed():
    matrix = line_matrix([0, 1000, 2000, 3000, 4000])
    result = planner.solve(matrix, [0, 6, 6, 6, 6], [10, 10], service_s=0, horizon_s=100_000, solver_seconds=1)
    served = [n for r in result["routes"] for n in r]
    assert len(served) == 2 and len(result["dropped"]) == 2  # 6 + 6 > 10: one stop per vehicle
    assert all(sum([0, 6, 6, 6, 6][n] for n in r) <= 10 for r in result["routes"])
    assert sorted(served + result["dropped"]) == [1, 2, 3, 4]


def test_working_hours_limit_what_a_vehicle_can_do():
    matrix = line_matrix([0, 1000, 2000, 3000])  # each leg 100 s
    # service 600 s each: a day of 1500 s allows the round trip to the nearest stop only.
    result = planner.solve(matrix, [0, 1, 1, 1], [10], service_s=600, horizon_s=1500, solver_seconds=1)
    assert len(result["routes"][0]) == 1
    assert len(result["dropped"]) == 2


def test_arrival_times_account_for_travel_and_service():
    matrix = line_matrix([0, 1000, 2000])
    result = planner.solve(matrix, [0, 1, 1], [10], service_s=60, horizon_s=10_000, solver_seconds=1)
    first, second = result["routes"][0]
    assert result["arrivals"][first] == 100  # 1000 m at 10 m/s
    assert result["arrivals"][second] == 100 + 60 + 100


def test_nothing_to_deliver_gives_empty_routes():
    result = planner.solve(line_matrix([0]), [0], [10, 10], service_s=60, horizon_s=1000)
    assert result["routes"] == [[], []] and result["dropped"] == []


def test_the_50_stop_benchmark_meets_the_targets():
    """Targets of the charter: a 50-stop plan in under 10 s and at least 15 % less distance than the baseline."""
    row = benchmark.run(stops=50, seed=1, solver_seconds=3)
    assert row["served"] == 50 and row["dropped"] == 0
    assert row["elapsed_seconds"] < 10
    assert row["gain_pct"] >= 15


@pytest.mark.parametrize("seed", [2, 3])
def test_the_gain_holds_on_other_random_sets(seed):
    assert benchmark.run(stops=50, seed=seed, solver_seconds=2)["gain_pct"] >= 15
