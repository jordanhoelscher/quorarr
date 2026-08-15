import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from pensieve.auth import SESSION_COOKIE, sign_session
from pensieve.clients.base import CachedHTTP
from pensieve.main import create_app
from pensieve.services import library
from tests.conftest import make_settings, seed_user

FIXTURES = Path(__file__).parent / "fixtures"
RAW_MOVIES = json.loads((FIXTURES / "radarr_movie.json").read_text())
RAW_SERIES = json.loads((FIXTURES / "sonarr_series.json").read_text())
RAW_EPISODE_FILES = json.loads((FIXTURES / "sonarr_episodefiles.json").read_text())

# --- Pure shaping tests -------------------------------------------------


def _movie_row(**overrides):
    base = {
        "arr_id": 101,
        "title": "Arrival",
        "year": 2016,
        "tmdb_id": 329865,
        "size_bytes": 8589934592,
        "quality": "Bluray-1080p",
        "resolution": 1080,
        "profile_id": 6,
        "poster": "https://example/poster.jpg",
        "added": "2024-01-15T12:00:00Z",
        "has_file": True,
    }
    base.update(overrides)
    return base


def _series_row(**overrides):
    base = {
        "arr_id": 201,
        "title": "The Bear",
        "year": 2022,
        "tvdb_id": 371980,
        "size_bytes": 10737418240,
        "episode_count": 18,
        "profile_id": 4,
        "poster": "https://example/poster.jpg",
        "added": "2023-06-01T00:00:00Z",
        "seasons": [
            {"season_number": 1, "size_bytes": 5368709120, "episode_file_count": 8, "monitored": True},
            {"season_number": 2, "size_bytes": 5368709120, "episode_file_count": 10, "monitored": True},
        ],
    }
    base.update(overrides)
    return base


def _file(**overrides):
    base = {"id": 3001, "season_number": 1, "size_bytes": 671088640, "quality": "WEBDL-1080p", "resolution": 1080}
    base.update(overrides)
    return base


def test_movies_drops_profile_id_adds_media_type_keeps_other_keys():
    rows = library.movies([_movie_row()])

    assert len(rows) == 1
    row = rows[0]
    assert "profile_id" not in row
    assert row["media_type"] == "movie"
    assert row["arr_id"] == 101
    assert row["title"] == "Arrival"
    assert row["quality"] == "Bluray-1080p"
    assert row["has_file"] is True


def test_movies_preserves_order_and_count():
    rows = library.movies([_movie_row(arr_id=1), _movie_row(arr_id=2), _movie_row(arr_id=3)])
    assert [r["arr_id"] for r in rows] == [1, 2, 3]


def test_series_list_drops_profile_id_adds_media_type_keeps_seasons():
    rows = library.series_list([_series_row()])

    assert len(rows) == 1
    row = rows[0]
    assert "profile_id" not in row
    assert row["media_type"] == "series"
    assert row["arr_id"] == 201
    # Rollup rows carry seasons through untouched -- no quality mix at this level.
    assert row["seasons"] == [
        {"season_number": 1, "size_bytes": 5368709120, "episode_file_count": 8, "monitored": True},
        {"season_number": 2, "size_bytes": 5368709120, "episode_file_count": 10, "monitored": True},
    ]


def test_series_detail_builds_quality_mix_per_season():
    row = _series_row(seasons=[
        {"season_number": 1, "size_bytes": 1342177280, "episode_file_count": 2, "monitored": True},
        {"season_number": 2, "size_bytes": 536870912, "episode_file_count": 1, "monitored": True},
    ])
    files = [
        _file(id=3001, season_number=1, quality="WEBDL-1080p"),
        _file(id=3002, season_number=1, quality="WEBDL-1080p"),
        _file(id=3003, season_number=2, quality="WEBDL-720p"),
    ]

    detail = library.series_detail(row, files)

    assert detail["media_type"] == "series"
    assert "profile_id" not in detail
    seasons_by_number = {s["season_number"]: s for s in detail["seasons"]}
    assert seasons_by_number[1]["qualities"] == {"WEBDL-1080p": 2}
    assert seasons_by_number[2]["qualities"] == {"WEBDL-720p": 1}
    # Other season fields survive untouched.
    assert seasons_by_number[1]["episode_file_count"] == 2
    assert seasons_by_number[1]["monitored"] is True


def test_series_detail_quality_mix_counts_multiple_distinct_qualities():
    row = _series_row(seasons=[
        {"season_number": 1, "size_bytes": 100, "episode_file_count": 4, "monitored": True},
    ])
    files = [
        _file(id=1, season_number=1, quality="Bluray-1080p"),
        _file(id=2, season_number=1, quality="Bluray-1080p"),
        _file(id=3, season_number=1, quality="HDTV-720p"),
        _file(id=4, season_number=1, quality="Bluray-1080p"),
    ]

    detail = library.series_detail(row, files)

    assert detail["seasons"][0]["qualities"] == {"Bluray-1080p": 3, "HDTV-720p": 1}


def test_series_detail_none_quality_counts_as_unknown():
    row = _series_row(seasons=[
        {"season_number": 1, "size_bytes": 100, "episode_file_count": 2, "monitored": True},
    ])
    files = [
        _file(id=1, season_number=1, quality=None),
        _file(id=2, season_number=1, quality="WEBDL-1080p"),
    ]

    detail = library.series_detail(row, files)

    assert detail["seasons"][0]["qualities"] == {"unknown": 1, "WEBDL-1080p": 1}


def test_series_detail_season_with_no_files_gets_empty_qualities():
    row = _series_row(seasons=[
        {"season_number": 1, "size_bytes": 100, "episode_file_count": 2, "monitored": True},
        {"season_number": 2, "size_bytes": 0, "episode_file_count": 0, "monitored": True},
    ])
    files = [_file(id=1, season_number=1, quality="WEBDL-1080p")]

    detail = library.series_detail(row, files)

    seasons_by_number = {s["season_number"]: s for s in detail["seasons"]}
    assert seasons_by_number[2]["qualities"] == {}


def test_series_detail_does_not_mutate_input_row():
    row = _series_row(seasons=[
        {"season_number": 1, "size_bytes": 100, "episode_file_count": 1, "monitored": True},
    ])
    files = [_file(id=1, season_number=1, quality="WEBDL-1080p")]

    library.series_detail(row, files)

    assert "qualities" not in row["seasons"][0]
    assert "profile_id" in row


# --- Route tests: GET /api/library/movies, /series, /series/{id}, POST /refresh ----


def _healthy_route(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v3/movie":
        return httpx.Response(200, json=RAW_MOVIES)
    if request.url.path == "/api/v3/series":
        return httpx.Response(200, json=RAW_SERIES)
    if request.url.path == "/api/v3/episodefile":
        return httpx.Response(200, json=RAW_EPISODE_FILES)
    return httpx.Response(404)


def _radarr_500_route(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v3/movie":
        return httpx.Response(500, text="boom")
    if request.url.path == "/api/v3/series":
        return httpx.Response(200, json=RAW_SERIES)
    if request.url.path == "/api/v3/episodefile":
        return httpx.Response(200, json=RAW_EPISODE_FILES)
    return httpx.Response(404)


def _sonarr_500_route(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v3/movie":
        return httpx.Response(200, json=RAW_MOVIES)
    if request.url.path in ("/api/v3/series", "/api/v3/episodefile"):
        return httpx.Response(500, text="boom")
    return httpx.Response(404)


class _CountingTransport(httpx.AsyncBaseTransport):
    """Routes requests via a swappable function and counts calls per path."""

    def __init__(self, route):
        self.route = route
        self.calls: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls[request.url.path] = self.calls.get(request.url.path, 0) + 1
        return self.route(request)


def _make_client(tmp_path, route, **overrides) -> tuple[TestClient, object, _CountingTransport]:
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    transport = _CountingTransport(route)
    app.state.http = CachedHTTP(httpx.AsyncClient(transport=transport))
    return client, settings, transport


def _login(client: TestClient, settings) -> None:
    # current_user is DB-authoritative, so the users row has to exist too.
    seed_user(settings, user_id=1, name="Sam", role="member")
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": 1, "name": "Sam", "role": "member"})
    )


def test_library_movies_requires_auth(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _healthy_route)
    try:
        resp = client.get("/api/library/movies")
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_library_movies_happy_path(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _healthy_route)
    try:
        _login(client, settings)
        resp = client.get("/api/library/movies")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == len(RAW_MOVIES)
        assert all(i["media_type"] == "movie" for i in items)
        assert all("profile_id" not in i for i in items)
        arrival = next(i for i in items if i["arr_id"] == 101)
        assert arrival["title"] == "Arrival"
        assert arrival["quality"] == "Bluray-1080p"
    finally:
        client.__exit__(None, None, None)


def test_library_series_happy_path(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _healthy_route)
    try:
        _login(client, settings)
        resp = client.get("/api/library/series")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == len(RAW_SERIES)
        assert all(i["media_type"] == "series" for i in items)
        assert all("profile_id" not in i for i in items)
        bear = next(i for i in items if i["arr_id"] == 201)
        assert bear["title"] == "The Bear"
        assert "seasons" in bear
    finally:
        client.__exit__(None, None, None)


def test_library_movies_502_when_radarr_down(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _radarr_500_route)
    try:
        _login(client, settings)
        resp = client.get("/api/library/movies")
        assert resp.status_code == 502
        assert "radarr" in resp.json()["error"]
    finally:
        client.__exit__(None, None, None)


def test_library_series_detail_happy_path_has_qualities(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _healthy_route)
    try:
        _login(client, settings)
        resp = client.get("/api/library/series/201")
        assert resp.status_code == 200
        body = resp.json()
        assert body["arr_id"] == 201
        assert body["media_type"] == "series"
        assert "profile_id" not in body
        seasons_by_number = {s["season_number"]: s for s in body["seasons"]}
        # Two WEBDL-1080p files in season 1, one WEBDL-720p in season 2 (fixture).
        assert seasons_by_number[1]["qualities"] == {"WEBDL-1080p": 2}
        assert seasons_by_number[2]["qualities"] == {"WEBDL-720p": 1}
    finally:
        client.__exit__(None, None, None)


def test_library_series_detail_404_when_not_found(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _healthy_route)
    try:
        _login(client, settings)
        resp = client.get("/api/library/series/999999")
        assert resp.status_code == 404
        assert resp.json() == {"error": "series not found"}
    finally:
        client.__exit__(None, None, None)


def test_library_series_detail_502_when_sonarr_down(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _sonarr_500_route)
    try:
        _login(client, settings)
        resp = client.get("/api/library/series/201")
        assert resp.status_code == 502
        assert "sonarr" in resp.json()["error"]
    finally:
        client.__exit__(None, None, None)


def test_library_refresh_forces_refetch_on_next_get(tmp_path):
    client, settings, transport = _make_client(tmp_path, _healthy_route)
    try:
        _login(client, settings)
        warm = client.get("/api/library/movies")
        assert warm.status_code == 200
        assert transport.calls["/api/v3/movie"] == 1

        # Second GET within the TTL window should be served from cache -- no refetch.
        again = client.get("/api/library/movies")
        assert again.status_code == 200
        assert transport.calls["/api/v3/movie"] == 1

        refresh = client.post("/api/library/refresh")
        assert refresh.status_code == 200
        assert refresh.json() == {"ok": True}

        after_refresh = client.get("/api/library/movies")
        assert after_refresh.status_code == 200
        assert transport.calls["/api/v3/movie"] == 2
    finally:
        client.__exit__(None, None, None)


def test_library_refresh_requires_auth(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _healthy_route)
    try:
        resp = client.post("/api/library/refresh")
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)
