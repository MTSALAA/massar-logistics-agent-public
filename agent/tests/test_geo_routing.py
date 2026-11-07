"""Geocoding (Nominatim, cached, polite) and the distance matrix (OSRM with straight-line fallback)."""
import time

import httpx
import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    for name in ("NOMINATIM_URL", "NOMINATIM_CONTACT", "NOMINATIM_COUNTRY", "OSRM_URL"):
        monkeypatch.delenv(name, raising=False)
    from app import db, geo, routing

    monkeypatch.setattr(geo, "min_interval", 0)
    return db, geo, routing, monkeypatch


# --------------------------------------------------------------------------- haversine
def test_haversine_known_distances(env):
    _, geo, _, _ = env
    assert geo.haversine_m(48.8566, 2.3522, 51.5074, -0.1278) == pytest.approx(343_500, rel=0.01)  # Paris-London
    assert geo.haversine_m(36.75, 5.05, 36.75, 5.05) == 0
    assert geo.haversine_m(0, 0, 0, 180) == pytest.approx(20_015_000, rel=0.001)  # half the Earth


# --------------------------------------------------------------------------- geocoding
class FakeNominatim:
    def __init__(self, answer=None, status=200):
        self.answer = [{"lat": "36.7525", "lon": "5.0556", "display_name": "x"}] if answer is None else answer
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json=self.answer)


def use(env, fake, url="https://nominatim.test"):
    _, geo, _, monkeypatch = env
    monkeypatch.setenv("NOMINATIM_URL", url)
    monkeypatch.setattr(geo, "transport", httpx.MockTransport(fake))


def test_geocoding_is_off_unless_configured(env):
    _, geo, _, _ = env
    assert geo.geocode("A", "12 rue X").status == "disabled"


def test_geocode_asks_nominatim_politely_and_caches(env):
    db, geo, _, monkeypatch = env
    fake = FakeNominatim()
    use(env, fake)
    monkeypatch.setenv("NOMINATIM_CONTACT", "team@example.org")
    monkeypatch.setenv("NOMINATIM_COUNTRY", "dz")

    first = geo.geocode("A", "  12 Rue  des Freres, Bejaia ")
    assert (first.lat, first.lon, first.status) == (36.7525, 5.0556, "found")
    request = fake.requests[0]
    assert request.url.params["q"] == "  12 Rue  des Freres, Bejaia "
    assert request.url.params["format"] == "jsonv2" and request.url.params["limit"] == "1"
    assert request.url.params["countrycodes"] == "dz"
    assert "MassarLogisticsAgent" in request.headers["User-Agent"] and "team@example.org" in request.headers["User-Agent"]

    again = geo.geocode("A", "12 rue des freres, bejaia")  # same address, other spelling of case and spaces
    assert (again.lat, again.status) == (36.7525, "cache")
    assert len(fake.requests) == 1


def test_an_unknown_address_is_cached_but_a_failure_is_not(env):
    _, geo, _, _ = env
    nothing = FakeNominatim(answer=[])
    use(env, nothing)
    assert geo.geocode("A", "nowhere at all").status == "not_found"
    assert geo.geocode("A", "nowhere at all").status == "not_found"
    assert len(nothing.requests) == 1  # the negative answer is remembered

    broken = FakeNominatim(status=503)
    use(env, broken)
    assert geo.geocode("A", "another place").status == "unavailable"
    assert geo.geocode("A", "another place").status == "unavailable"
    assert len(broken.requests) == 2  # a failure is retried next time


def test_garbled_answers_are_treated_as_unavailable(env):
    _, geo, _, _ = env
    use(env, FakeNominatim(answer=[{"lat": "abc", "lon": "5"}]))
    assert geo.geocode("A", "weird").status == "unavailable"


def test_the_cache_belongs_to_each_company(env):
    db, geo, _, _ = env
    fake = FakeNominatim()
    use(env, fake)
    geo.geocode("A", "same address")
    geo.geocode("B", "same address")
    assert len(fake.requests) == 2  # B never reads A's cache
    db.purge_tenant("A")
    assert db.geocache_get("A", "same address") is None and db.geocache_get("B", "same address") is not None


def test_requests_to_nominatim_are_spaced(env):
    _, geo, _, monkeypatch = env
    monkeypatch.setattr(geo, "min_interval", 0.3)
    use(env, FakeNominatim())
    started = time.monotonic()
    geo.geocode("A", "first")
    geo.geocode("A", "second")
    assert time.monotonic() - started >= 0.3


# --------------------------------------------------------------------------- matrix
POINTS = [(36.75, 5.05), (36.76, 5.06), (36.74, 5.04)]


def test_without_osrm_the_matrix_uses_straight_lines(env):
    _, _, routing, _ = env
    matrix = routing.build_matrix(POINTS)
    assert matrix.source == "straight_line" and "OSRM_URL" in matrix.note
    assert [matrix.distance_m[i][i] for i in range(3)] == [0, 0, 0]
    assert matrix.distance_m[0][1] == matrix.distance_m[1][0] > 1000
    assert matrix.duration_s[0][1] == pytest.approx(matrix.distance_m[0][1] / (35 / 3.6), abs=1)


def osrm_answer(request, durations=None, distances=None, code="Ok"):
    return httpx.Response(200, json={
        "code": code,
        "durations": durations or [[0, 60, 120], [61, 0, 90], [121, 91, 0]],
        "distances": distances or [[0, 700, 1500], [710, 0, 1100], [1510, 1110, 0]],
    })


def test_osrm_matrix_is_used_when_available_with_lon_lat_order(env):
    _, _, routing, monkeypatch = env
    seen = []
    monkeypatch.setenv("OSRM_URL", "http://osrm.test/")
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(lambda r: (seen.append(r), osrm_answer(r))[1]))
    matrix = routing.build_matrix(POINTS)
    assert matrix.source == "osrm"
    assert matrix.distance_m[0][2] == 1500 and matrix.duration_s[1][0] == 61
    path = seen[0].url.path
    assert path == "/table/v1/driving/5.050000,36.750000;5.060000,36.760000;5.040000,36.740000"  # lon,lat
    assert seen[0].url.params["annotations"] == "duration,distance"


def test_unreachable_pairs_become_very_far(env):
    _, _, routing, monkeypatch = env
    monkeypatch.setenv("OSRM_URL", "http://osrm.test")
    answer = lambda r: osrm_answer(r, durations=[[0, None, 1], [1, 0, 1], [1, 1, 0]],  # noqa: E731
                                   distances=[[0, None, 1], [1, 0, 1], [1, 1, 0]])
    monkeypatch.setattr(routing, "transport", httpx.MockTransport(answer))
    matrix = routing.build_matrix(POINTS)
    assert matrix.source == "osrm" and matrix.distance_m[0][1] >= 1_000_000


@pytest.mark.parametrize("failure", ["down", "http500", "badcode", "wrongsize", "notjson"])
def test_osrm_failures_fall_back_to_straight_lines(env, failure):
    _, _, routing, monkeypatch = env
    monkeypatch.setenv("OSRM_URL", "http://osrm.test")

    def handler(request):
        if failure == "down":
            raise httpx.ConnectError("refused")
        if failure == "http500":
            return httpx.Response(500, json={})
        if failure == "badcode":
            return osrm_answer(request, code="NoTable")
        if failure == "wrongsize":
            return osrm_answer(request, durations=[[0]], distances=[[0]])
        return httpx.Response(200, content=b"<html>")

    monkeypatch.setattr(routing, "transport", httpx.MockTransport(handler))
    matrix = routing.build_matrix(POINTS)
    assert matrix.source == "straight_line" and "OSRM unavailable" in matrix.note
    assert matrix.distance_m[0][1] > 0
