import json
import os
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from pensieve.auth import SESSION_COOKIE, sign_session
from pensieve.clients.base import CachedHTTP
from pensieve.main import create_app
from tests.conftest import make_settings, seed_user

FIXTURES = Path(__file__).parent / "fixtures"
MOVIES = json.loads((FIXTURES / "radarr_movie.json").read_text())
SERIES = json.loads((FIXTURES / "sonarr_series.json").read_text())

# f_frsize * f_blocks = total, f_frsize * f_bavail = free.
_STATVFS = os.statvfs_result(
    (4096, 4096, 250_000_000, 200_000_000, 100_000_000, 0, 0, 0, 0, 255)
)
_TOTAL_BYTES = 4096 * 250_000_000
_FREE_BYTES = 4096 * 100_000_000
_USED_BYTES = _TOTAL_BYTES - _FREE_BYTES

_MOVIES_BYTES = sum(m["sizeOnDisk"] for m in MOVIES)
_TV_BYTES = sum(s["statistics"]["sizeOnDisk"] for s in SERIES)


def _healthy_route(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v3/movie":
        return httpx.Response(200, json=MOVIES)
    if request.url.path == "/api/v3/series":
        return httpx.Response(200, json=SERIES)
    return httpx.Response(404)


def _radarr_500_route(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v3/movie":
        return httpx.Response(500, text="boom")
    if request.url.path == "/api/v3/series":
        return httpx.Response(200, json=SERIES)
    return httpx.Response(404)


class _ToggleTransport(httpx.AsyncBaseTransport):
    """A transport whose routing function can be swapped mid-test.

    Lets a test prime a warm ``CachedHTTP`` cache against a healthy backend,
    then flip to a failing backend on the *same* ``CachedHTTP`` instance so
    the previously-cached entries are still there for ``stale()`` to find.
    """

    def __init__(self, route):
        self.route = route

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self.route(request)


def _make_client(tmp_path, route, **overrides) -> tuple[TestClient, object, _ToggleTransport]:
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    transport = _ToggleTransport(route)
    app.state.http = CachedHTTP(httpx.AsyncClient(transport=transport))
    return client, settings, transport


def _login(client: TestClient, settings) -> None:
    # current_user is DB-authoritative, so the users row has to exist too.
    seed_user(settings, user_id=1, name="Sam", role="member")
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": 1, "name": "Sam", "role": "member"})
    )


def test_storage_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "statvfs", lambda path: _STATVFS)
    client, settings, _transport = _make_client(tmp_path, _healthy_route)
    try:
        resp = client.get("/api/storage")
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_storage_happy_path_math(tmp_path, monkeypatch):
    captured_path = []
    monkeypatch.setattr(os, "statvfs", lambda path: (captured_path.append(path), _STATVFS)[1])

    client, settings, _transport = _make_client(tmp_path, _healthy_route)
    try:
        _login(client, settings)
        resp = client.get("/api/storage")
        assert resp.status_code == 200
        assert captured_path == [settings.media_mount]
        assert resp.json() == {
            "total_bytes": _TOTAL_BYTES,
            "used_bytes": _USED_BYTES,
            "free_bytes": _FREE_BYTES,
            "movies_bytes": _MOVIES_BYTES,
            "tv_bytes": _TV_BYTES,
            "movie_count": len(MOVIES),
            "series_count": len(SERIES),
        }
    finally:
        client.__exit__(None, None, None)


def test_storage_falls_back_to_stale_when_radarr_down_but_cache_warm(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "statvfs", lambda path: _STATVFS)

    # list_movies/list_series use a 600s TTL, so a same-tick second call would
    # be served straight from the fresh cache without ever exercising the
    # fallback path. Fake the clock forward past the TTL so the second call
    # actually attempts (and fails) a live request, forcing stale() fallback.
    clock = {"t": 1_000.0}
    monkeypatch.setattr("pensieve.clients.base.time.monotonic", lambda: clock["t"])

    client, settings, transport = _make_client(tmp_path, _healthy_route)
    try:
        _login(client, settings)
        # Prime the cache with a fully successful call first.
        warm = client.get("/api/storage")
        assert warm.status_code == 200

        # Flip radarr to failing and advance past the TTL window; the cache
        # (same CachedHTTP instance, same app.state.http) still holds both
        # prior successful responses for stale() to fall back on.
        transport.route = _radarr_500_route
        clock["t"] += 700

        resp = client.get("/api/storage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["movies_bytes"] == _MOVIES_BYTES
        assert body["tv_bytes"] == _TV_BYTES
        assert body["movie_count"] == len(MOVIES)
        assert body["series_count"] == len(SERIES)
        assert body["total_bytes"] == _TOTAL_BYTES
        assert "stale_seconds" in body
        assert body["stale_seconds"] == 700
    finally:
        client.__exit__(None, None, None)


def test_storage_502_when_radarr_down_and_cache_cold(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "statvfs", lambda path: _STATVFS)

    client, settings, _transport = _make_client(tmp_path, _radarr_500_route)
    try:
        _login(client, settings)
        resp = client.get("/api/storage")
        assert resp.status_code == 502
        assert "radarr" in resp.json()["error"]
    finally:
        client.__exit__(None, None, None)
