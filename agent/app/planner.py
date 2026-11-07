"""Vehicle routing with Google OR-Tools: pure functions, no database, no network.

Problem solved (a capacitated vehicle routing problem with working hours):
  - node 0 is the depot; every route starts and ends there;
  - each stop has a demand (units to deliver); a vehicle cannot carry more than its capacity;
  - serving a stop takes `service_s` seconds; a vehicle must be back at the depot before the end of the
    working day (`horizon_s` seconds after the start of the day);
  - the cost to minimise is the total distance driven;
  - a stop that cannot be served (no vehicle has room, or not enough time) is dropped with a very high
    penalty instead of making the whole problem unsolvable, and is reported as unassigned.

The baseline used to measure the gain is the "order-entry sequence": stops are taken in the order they were
entered and vehicles are filled one after the other, with no optimisation. See docs/how-it-works/.
"""
import time

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .routing import Matrix

DEFAULT_SOLVER_SECONDS = 5
MAX_SOLVER_SECONDS = 30
DROP_PENALTY = 100_000_000  # per dropped stop, far above the length of any real route (in metres)


def solve(
    matrix: Matrix,
    demands: list[int],
    capacities: list[int],
    service_s: int,
    horizon_s: int,
    solver_seconds: float = DEFAULT_SOLVER_SECONDS,
) -> dict:
    """Optimise the routes. Node 0 of `matrix` is the depot; `demands[0]` must be 0.

    Returns {"routes": [[node, ...] per vehicle, depot excluded], "arrivals": {node: seconds since the start
    of the day}, "dropped": [node, ...], "compute_seconds": float}.
    """
    started = time.perf_counter()
    n, vehicles = len(demands), len(capacities)
    if n <= 1:
        return {"routes": [[] for _ in range(vehicles)], "arrivals": {}, "dropped": [], "compute_seconds": 0.0}

    manager = pywrapcp.RoutingIndexManager(n, vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance(i, j):
        return matrix.distance_m[manager.IndexToNode(i)][manager.IndexToNode(j)]

    def travel_and_service(i, j):
        a, b = manager.IndexToNode(i), manager.IndexToNode(j)
        return matrix.duration_s[a][b] + (service_s if a != 0 else 0)

    distance_cb = routing.RegisterTransitCallback(distance)
    routing.SetArcCostEvaluatorOfAllVehicles(distance_cb)

    time_cb = routing.RegisterTransitCallback(travel_and_service)
    routing.AddDimension(time_cb, 0, horizon_s, True, "Time")

    demand_cb = routing.RegisterUnaryTransitCallback(lambda i: demands[manager.IndexToNode(i)])
    routing.AddDimensionWithVehicleCapacity(demand_cb, 0, capacities, True, "Load")

    for node in range(1, n):
        routing.AddDisjunction([manager.NodeToIndex(node)], DROP_PENALTY)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromMilliseconds(int(max(0.2, min(solver_seconds, MAX_SOLVER_SECONDS)) * 1000))

    solution = routing.SolveWithParameters(params)
    if solution is None:
        return {
            "routes": [[] for _ in range(vehicles)], "arrivals": {}, "dropped": list(range(1, n)),
            "compute_seconds": round(time.perf_counter() - started, 3),
        }

    time_dim = routing.GetDimensionOrDie("Time")
    routes: list[list[int]] = []
    arrivals: dict[int, int] = {}
    for v in range(vehicles):
        index, route = routing.Start(v), []
        index = solution.Value(routing.NextVar(index))
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            route.append(node)
            arrivals[node] = solution.Min(time_dim.CumulVar(index))
            index = solution.Value(routing.NextVar(index))
        routes.append(route)
    served = {node for route in routes for node in route}
    return {
        "routes": routes,
        "arrivals": arrivals,
        "dropped": [node for node in range(1, n) if node not in served],
        "compute_seconds": round(time.perf_counter() - started, 3),
    }


def route_distance(matrix: Matrix, route: list[int]) -> int:
    """Length of depot -> stops -> depot, in metres (0 for an empty route)."""
    if not route:
        return 0
    path = [0, *route, 0]
    return sum(matrix.distance_m[a][b] for a, b in zip(path, path[1:]))


def baseline_routes(order: list[int], demands: list[int], capacities: list[int]) -> list[list[int]]:
    """Stops in the order they were entered, vehicles filled one after the other (no optimisation).

    A stop goes to the current vehicle if it still fits, otherwise the next vehicle is used. If even the
    last vehicle is full the stop is added to it anyway (the baseline ignores working hours and overloads
    rather than leaving a stop unserved, so the comparison covers the same stops as the optimised plan).
    """
    routes: list[list[int]] = [[] for _ in capacities]
    loads = [0] * len(capacities)
    v = 0
    for node in order:
        while v < len(capacities) - 1 and loads[v] + demands[node] > capacities[v]:
            v += 1
        routes[v].append(node)
        loads[v] += demands[node]
    return routes


def baseline_distance(matrix: Matrix, order: list[int], demands: list[int], capacities: list[int]) -> int:
    return sum(route_distance(matrix, r) for r in baseline_routes(order, demands, capacities))
