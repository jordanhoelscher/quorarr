"""Tests for the member-facing Discover routes (browse, search, detail, request)."""
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pensieve.auth import SESSION_COOKIE, sign_session
from pensieve.clients.base import CachedHTTP
from pensieve.db import connect
from pensieve.main import create_app
from pensieve.ratelimit import request_limiter
from tests.conftest import make_settings, seed_user

FIXTURES = Path(__file__).parent / "fixtures"
TRENDING = json.loads((FIXTURES / "jellyseerr_discover_trending.json").read_text())
SEARCH = json.loads((FIXTURES / "jellyseerr_search.json").read_text())
MOVIE_DETAIL = json.loads((FIXTURES / "jellyseerr_movie_detail.json").read_text())
TV_DETAIL = json.loads((FIXTURES / "jellyseerr_tv_detail.json").read_text())
USERS = json.loads((FIXTURES / "jellyseerr_users.json").read_text())
PERSON = json.loads((FIXTURES / "jellyseerr_person.json").read_text())
CREDITS = json.loads((FIXTURES / "jellyseerr_person_credits.json").read_text())

#: The seeded session user's Plex account id, matching the users fixture's
#: second row (jellyseerr user 4).
PLEX_ID = 222222


class _Router:
    """Records every outgoing request; returns canned responses by (method, path)."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.routes.get((request.method, request.url.path))
        if response is None:
            return httpx.Response(404)
        # A canned Response is consumed once; re-serve a fresh copy so a
        # route that calls the same endpoint twice still works.
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    def bodies(self, method: str, path: str) -> list:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == method and r.url.path == path
        ]


def _make_client(tmp_path, routes: dict, **overrides):
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    router = _Router(routes)
    app.state.http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(router)))
    return client, settings, router


def _login(client: TestClient, settings, *, user_id: int = PLEX_ID, name: str = "Sam") -> None:
    seed_user(settings, user_id=user_id, name=name, role="member")
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": user_id, "name": name, "role": "member"})
    )


@pytest.fixture(autouse=True)
def _reset_limiter():
    """The request limiter is process-global; keep tests from leaking into each other."""
    request_limiter._hits.clear()
    yield
    request_limiter._hits.clear()


_BROWSE_ROUTES = {
    ("GET", "/api/v1/discover/trending"): httpx.Response(200, json=TRENDING),
    ("GET", "/api/v1/discover/movies"): httpx.Response(200, json=TRENDING),
    ("GET", "/api/v1/discover/movies/upcoming"): httpx.Response(200, json=TRENDING),
    ("GET", "/api/v1/search"): httpx.Response(200, json=SEARCH),
    ("GET", "/api/v1/movie/550"): httpx.Response(200, json=MOVIE_DETAIL),
    ("GET", "/api/v1/tv/1399"): httpx.Response(200, json=TV_DETAIL),
    ("GET", "/api/v1/user"): httpx.Response(200, json=USERS),
    ("POST", "/api/v1/request"): httpx.Response(201, json={"id": 88, "status": 1}),
    ("GET", "/api/v1/person/31"): httpx.Response(200, json=PERSON),
    ("GET", "/api/v1/person/31/combined_credits"): httpx.Response(200, json=CREDITS),
}


# --- auth --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/discover/shelves"),
        ("GET", "/api/discover/search?q=matrix"),
        ("GET", "/api/discover/detail/movie/550"),
        ("GET", "/api/discover/suggest?q=matrix"),
        ("GET", "/api/discover/person/31"),
        ("POST", "/api/discover/request"),
    ],
)
def test_discover_requires_a_session(tmp_path, method, path):
    client, _settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        assert client.request(method, path, json={}).status_code == 401
    finally:
        client.__exit__(None, None, None)


# --- shelves -----------------------------------------------------------------


def test_shelves_200(tmp_path):
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/shelves")
        assert resp.status_code == 200
        shelves = resp.json()["shelves"]
        assert [s["id"] for s in shelves] == ["trending", "popular", "upcoming"]
        card = shelves[0]["items"][0]
        assert set(card) == {
            "tmdb_id", "title", "year", "media_type", "poster_path",
            "overview", "rating", "status", "availability",
        }
    finally:
        client.__exit__(None, None, None)


def test_shelves_502_when_jellyseerr_is_down(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        resp = client.get("/api/discover/shelves")
        assert resp.status_code == 502
        assert resp.json() == {"error": "jellyseerr unreachable"}
    finally:
        client.__exit__(None, None, None)


# --- search ------------------------------------------------------------------


def test_search_200_drops_people(tmp_path):
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/search", params={"q": "matrix"})
        assert resp.status_code == 200
        assert [row["title"] for row in resp.json()["items"]] == ["The Matrix", "Threat Matrix"]
        sent = [r for r in router.requests if r.url.path == "/api/v1/search"]
        assert sent[0].url.params["query"] == "matrix"
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("q", ["", "x" * 101])
def test_search_422_on_an_out_of_bounds_query(tmp_path, q):
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        assert client.get("/api/discover/search", params={"q": q}).status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_search_502_when_jellyseerr_is_down(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        resp = client.get("/api/discover/search", params={"q": "matrix"})
        assert resp.status_code == 502
        assert resp.json() == {"error": "jellyseerr unreachable"}
    finally:
        client.__exit__(None, None, None)


# --- detail ------------------------------------------------------------------


def test_detail_movie_200(tmp_path):
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/detail/movie/550")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Fight Club"
        assert body["seasons"] is None
    finally:
        client.__exit__(None, None, None)


def test_detail_tv_200_carries_season_availability(tmp_path):
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        body = client.get("/api/discover/detail/tv/1399").json()
        assert [s["season_number"] for s in body["seasons"]] == [1, 2, 3, 4]
        assert [s["requestable"] for s in body["seasons"]] == [False, False, True, True]
    finally:
        client.__exit__(None, None, None)


def test_detail_422_on_an_unknown_media_type(tmp_path):
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        assert client.get("/api/discover/detail/person/550").status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_detail_404_when_jellyseerr_has_no_such_title(tmp_path):
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/detail/movie/999999")
        assert resp.status_code == 404
        assert resp.json() == {"error": "not found"}
    finally:
        client.__exit__(None, None, None)


# --- request -----------------------------------------------------------------


def test_request_movie_201_attributed_to_the_session_user(tmp_path):
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request", json={"media_type": "movie", "tmdb_id": 550}
        )
        assert resp.status_code == 201
        assert resp.json() == {"ok": True, "request_id": 88, "title": "Fight Club"}

        body = router.bodies("POST", "/api/v1/request")[0]
        # userId 4 is the fixture row whose plexId is the session's account id.
        # profileId 6 is the audited Radarr HD lane: films are pinned to it
        # explicitly rather than inheriting whatever default Jellyseerr has.
        assert body == {
            "mediaType": "movie", "mediaId": 550, "userId": 4,
            "profileId": settings.radarr_profile_hd_id,
        }
        assert "is4k" not in body

        conn = connect(settings.db_path)
        try:
            row = conn.execute(
                "SELECT actor, action, detail FROM events WHERE action = 'media_requested'"
            ).fetchone()
        finally:
            conn.close()
        assert row["actor"] == "Sam"
        assert "Fight Club" in row["detail"]
    finally:
        client.__exit__(None, None, None)


def test_request_tv_sends_only_the_picked_seasons(tmp_path):
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "tv", "tmdb_id": 1399, "seasons": [3, 4]},
        )
        assert resp.status_code == 201
        assert router.bodies("POST", "/api/v1/request")[0] == {
            "mediaType": "tv", "mediaId": 1399, "userId": 4, "seasons": [3, 4],
            "profileId": settings.sonarr_profile_hd_id,
        }
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("seasons", [[1], [2], [3, 2], []])
def test_request_tv_409_on_a_season_already_had_or_asked_for(tmp_path, seasons):
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "tv", "tmdb_id": 1399, "seasons": seasons},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]
        assert router.bodies("POST", "/api/v1/request") == []
    finally:
        client.__exit__(None, None, None)


def test_request_tv_409_when_no_seasons_are_sent_at_all(tmp_path):
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post("/api/discover/request", json={"media_type": "tv", "tmdb_id": 1399})
        assert resp.status_code == 409
        assert router.bodies("POST", "/api/v1/request") == []
    finally:
        client.__exit__(None, None, None)


def test_request_surfaces_jellyseerrs_duplicate_message(tmp_path):
    routes = {
        **_BROWSE_ROUTES,
        ("POST", "/api/v1/request"): httpx.Response(
            409, json={"message": "Request for this media already exists"}
        ),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request", json={"media_type": "movie", "tmdb_id": 550}
        )
        assert resp.status_code == 409
        assert resp.json() == {"error": "Request for this media already exists"}
    finally:
        client.__exit__(None, None, None)


def test_request_502_when_jellyseerr_errors(tmp_path):
    routes = {**_BROWSE_ROUTES, ("POST", "/api/v1/request"): httpx.Response(500)}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request", json={"media_type": "movie", "tmdb_id": 550}
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "jellyseerr unreachable"}
    finally:
        client.__exit__(None, None, None)


def test_request_502_with_a_human_message_when_the_account_cannot_be_mapped(tmp_path):
    """Never silently file it as the API key's owner -- say so and stop."""
    routes = {
        **_BROWSE_ROUTES,
        ("GET", "/api/v1/user"): httpx.Response(200, json={"results": []}),
        ("POST", "/api/v1/user/import-from-plex"): httpx.Response(201, json=[]),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request", json={"media_type": "movie", "tmdb_id": 550}
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "couldn't map your account — ask the owner"}
        assert router.bodies("POST", "/api/v1/request") == []
    finally:
        client.__exit__(None, None, None)


def test_request_tells_a_pending_member_to_accept_their_plex_invite(tmp_path):
    """The 502 said "ask the owner" to someone the owner had already approved.

    Same UserMappingError as the test above; the difference is that plex.tv
    reports an invite still sitting unaccepted, which the friend can fix.
    """
    routes = {
        **_BROWSE_ROUTES,
        ("GET", "/api/v1/user"): httpx.Response(200, json={"results": []}),
        ("POST", "/api/v1/user/import-from-plex"): httpx.Response(201, json=[]),
        ("GET", "/api/invites/requested"): httpx.Response(
            200,
            text='<?xml version="1.0"?><MediaContainer><Invite id="%d" createdAt="1" '
                 'username="x" email="x@example.com"/></MediaContainer>' % PLEX_ID,
        ),
        ("GET", "/api/servers/machine-123/shared_servers"): httpx.Response(
            200, text='<?xml version="1.0"?><MediaContainer/>'
        ),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request", json={"media_type": "movie", "tmdb_id": 550}
        )
        assert resp.status_code == 409
        assert "Plex invite" in resp.json()["error"]
        assert router.bodies("POST", "/api/v1/request") == []
    finally:
        client.__exit__(None, None, None)


def test_request_still_says_ask_the_owner_when_plex_is_unreadable(tmp_path):
    """An unknown share state must never be guessed into the 409."""
    routes = {
        **_BROWSE_ROUTES,
        ("GET", "/api/v1/user"): httpx.Response(200, json={"results": []}),
        ("POST", "/api/v1/user/import-from-plex"): httpx.Response(201, json=[]),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request", json={"media_type": "movie", "tmdb_id": 550}
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "couldn't map your account — ask the owner"}
    finally:
        client.__exit__(None, None, None)


def test_request_429_past_the_hourly_cap(tmp_path):
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        body = {"media_type": "movie", "tmdb_id": 550}
        seen = {client.post("/api/discover/request", json=body).status_code for _ in range(25)}
        assert 429 in seen
    finally:
        client.__exit__(None, None, None)


def test_request_rejects_a_4k_field_outright(tmp_path):
    """The body model is strict, so nobody can smuggle 4K past the house policy."""
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "movie", "tmdb_id": 550, "is4k": True},
        )
        assert resp.status_code == 422
    finally:
        client.__exit__(None, None, None)


# --- request quality ----------------------------------------------------------


def test_request_tv_720p_files_against_the_space_saver_lane(tmp_path):
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "tv", "tmdb_id": 1399, "seasons": [3], "quality": "720p"},
        )
        assert resp.status_code == 201
        body = router.bodies("POST", "/api/v1/request")[0]
        assert body["profileId"] == settings.sonarr_profile_720_id
        assert "is4k" not in body
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("extra", [{}, {"quality": "1080p"}])
def test_request_tv_defaults_to_the_1080_lane(tmp_path, extra):
    """Omitting quality must mean 1080p, not 'whatever Jellyseerr fancies'."""
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "tv", "tmdb_id": 1399, "seasons": [3], **extra},
        )
        assert resp.status_code == 201
        assert router.bodies("POST", "/api/v1/request")[0]["profileId"] == (
            settings.sonarr_profile_hd_id
        )
    finally:
        client.__exit__(None, None, None)


def test_request_tv_720p_502_when_the_lane_is_unconfigured(tmp_path):
    """Never quietly upgrade a space-saver pick to the premium lane."""
    client, settings, router = _make_client(
        tmp_path, _BROWSE_ROUTES, sonarr_profile_720_id=0
    )
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "tv", "tmdb_id": 1399, "seasons": [3], "quality": "720p"},
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "720p lane not configured"}
        assert router.bodies("POST", "/api/v1/request") == []
    finally:
        client.__exit__(None, None, None)


def test_request_movie_rejects_the_720_lane(tmp_path):
    """Films have no quality choice: 720p on a movie is a 422, not a shrug."""
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "movie", "tmdb_id": 550, "quality": "720p"},
        )
        assert resp.status_code == 422
        assert router.bodies("POST", "/api/v1/request") == []
    finally:
        client.__exit__(None, None, None)


def test_request_rejects_a_quality_outside_the_vocabulary(tmp_path):
    """No raw profile ids, no invented tiers -- the vocabulary is closed."""
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        for quality in ("2160p", "profileId", "", 5):
            resp = client.post(
                "/api/discover/request",
                json={"media_type": "movie", "tmdb_id": 550, "quality": quality},
            )
            assert resp.status_code == 422
        assert router.bodies("POST", "/api/v1/request") == []
    finally:
        client.__exit__(None, None, None)


# --- 4K: owner files, friend asks ---------------------------------------------


def _login_owner(client, settings) -> None:
    seed_user(settings, user_id=PLEX_ID, name="Ada", role="owner")
    client.cookies.set(
        SESSION_COOKIE,
        sign_session(settings, {"id": PLEX_ID, "name": "Ada", "role": "owner"}),
    )


def test_owner_4k_movie_files_immediately_against_the_4k_profile(tmp_path):
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login_owner(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "movie", "tmdb_id": 550, "quality": "4K"},
        )
        assert resp.status_code == 201
        body = router.bodies("POST", "/api/v1/request")[0]
        assert body["profileId"] == settings.radarr_profile_4k_id
        # The 4K *profile*, never Jellyseerr's own 4K switch.
        assert "is4k" not in body
    finally:
        client.__exit__(None, None, None)


def test_owner_4k_tv_files_immediately_against_the_4k_profile(tmp_path):
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login_owner(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "tv", "tmdb_id": 1399, "seasons": [3], "quality": "4K"},
        )
        assert resp.status_code == 201
        body = router.bodies("POST", "/api/v1/request")[0]
        assert body["profileId"] == settings.sonarr_profile_4k_id
        assert "is4k" not in body
    finally:
        client.__exit__(None, None, None)


def test_member_4k_parks_for_approval_and_files_nothing(tmp_path):
    """The whole point of the gate: no download happens before the owner says so."""
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "movie", "tmdb_id": 550, "quality": "4K"},
        )
        assert resp.status_code == 202
        assert resp.json()["state"] == "pending_approval"
        assert resp.json()["title"] == "Fight Club"
        assert router.bodies("POST", "/api/v1/request") == []

        conn = connect(settings.db_path)
        try:
            row = conn.execute("SELECT * FROM discover_4k_requests").fetchone()
            event = conn.execute(
                "SELECT actor, action FROM events WHERE action = 'discover_4k_requested'"
            ).fetchone()
        finally:
            conn.close()
        assert row["state"] == "pending"
        assert row["title"] == "Fight Club"
        assert row["requested_by"] == PLEX_ID
        assert row["requested_by_name"] == "Sam"
        assert row["seasons_json"] is None
        assert event["actor"] == "Sam"
    finally:
        client.__exit__(None, None, None)


def test_member_4k_tv_stores_the_exact_season_pick(tmp_path):
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "tv", "tmdb_id": 1399, "seasons": [4, 3], "quality": "4K"},
        )
        assert resp.status_code == 202

        conn = connect(settings.db_path)
        try:
            row = conn.execute("SELECT * FROM discover_4k_requests").fetchone()
        finally:
            conn.close()
        assert json.loads(row["seasons_json"]) == [3, 4]
    finally:
        client.__exit__(None, None, None)


def test_member_4k_still_refuses_a_season_already_on_the_server(tmp_path):
    """The approval lane is not a way around the season guard."""
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "tv", "tmdb_id": 1399, "seasons": [1], "quality": "4K"},
        )
        assert resp.status_code == 409

        conn = connect(settings.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) c FROM discover_4k_requests").fetchone()["c"] == 0
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


def test_member_4k_duplicate_pending_is_409(tmp_path):
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        body = {"media_type": "movie", "tmdb_id": 550, "quality": "4K"}
        assert client.post("/api/discover/request", json=body).status_code == 202
        second = client.post("/api/discover/request", json=body)
        assert second.status_code == 409
        assert second.json()["error"]

        conn = connect(settings.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) c FROM discover_4k_requests").fetchone()["c"] == 1
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


def test_member_4k_notifies_the_owner(tmp_path):
    """Discord is the fallback channel when the owner has no push registered."""
    routes = {
        **_BROWSE_ROUTES,
        ("POST", "/webhook"): httpx.Response(204),
    }
    client, settings, router = _make_client(
        tmp_path, routes, discord_webhook_url="https://discord.test/webhook"
    )
    try:
        seed_user(settings, user_id=999, name="Ada", role="owner")
        _login(client, settings)
        resp = client.post(
            "/api/discover/request",
            json={"media_type": "movie", "tmdb_id": 550, "quality": "4K"},
        )
        assert resp.status_code == 202
        posted = router.bodies("POST", "/webhook")
        assert len(posted) == 1
        assert "Fight Club" in posted[0]["content"]
        assert "Sam" in posted[0]["content"]
    finally:
        client.__exit__(None, None, None)


def test_request_relays_only_the_friend_facing_4xx(tmp_path):
    """400/404/409 carry Jellyseerr's wording; the friend can act on those."""
    for status in (400, 404, 409):
        routes = {
            **_BROWSE_ROUTES,
            ("POST", "/api/v1/request"): httpx.Response(
                status, json={"message": "no dice"}
            ),
        }
        client, settings, _router = _make_client(tmp_path, routes)
        try:
            _login(client, settings)
            resp = client.post(
                "/api/discover/request", json={"media_type": "movie", "tmdb_id": 550}
            )
            assert resp.status_code == status
            assert resp.json() == {"error": "no dice"}
        finally:
            client.__exit__(None, None, None)


@pytest.mark.parametrize("status", [401, 403, 429, 418])
def test_request_clamps_other_jellyseerr_4xx_to_502(tmp_path, status):
    """A 401 relayed verbatim would log the *friend* out of the app.

    ``api.ts`` treats any 401 as "session gone" and bounces to the login
    screen, so an unhappy Jellyseerr API key would read to a friend as being
    kicked out of the app for a fault that was never theirs.
    """
    routes = {
        **_BROWSE_ROUTES,
        ("POST", "/api/v1/request"): httpx.Response(
            status, json={"message": "Unauthorized: bad api key"}
        ),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request", json={"media_type": "movie", "tmdb_id": 550}
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "jellyseerr unreachable"}
        # And the upstream's wording never reaches the friend either.
        assert "api key" not in resp.text
    finally:
        client.__exit__(None, None, None)


def test_pending_4k_duplicate_is_blocked_by_the_database_too(tmp_path):
    """The index closes the SELECT-then-INSERT race the pre-check leaves open.

    Simulated by inserting the racing row directly (the pre-check saw
    nothing), which is exactly the state two concurrent requests reach.
    """
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        conn = connect(settings.db_path)
        try:
            conn.execute(
                """INSERT INTO discover_4k_requests
                   (media_type, tmdb_id, title, requested_by, requested_by_name,
                    state, created_at)
                   VALUES ('movie', 550, 'Fight Club', 1, 'Other', 'pending', 'now')"""
            )
            conn.commit()
            # The index refuses a second pending row for the same title.
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO discover_4k_requests
                       (media_type, tmdb_id, title, requested_by, requested_by_name,
                        state, created_at)
                       VALUES ('movie', 550, 'Fight Club', 2, 'Sam', 'pending', 'now')"""
                )
            # ...but a settled row does not block a fresh ask.
            conn.execute("UPDATE discover_4k_requests SET state = 'denied'")
            conn.execute(
                """INSERT INTO discover_4k_requests
                   (media_type, tmdb_id, title, requested_by, requested_by_name,
                    state, created_at)
                   VALUES ('movie', 550, 'Fight Club', 2, 'Sam', 'pending', 'now')"""
            )
            conn.commit()
        finally:
            conn.close()
    finally:
        client.__exit__(None, None, None)


def test_racing_4k_ask_answers_409_not_500(tmp_path):
    """The route turns the index's IntegrityError into the ordinary 409."""
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        conn = connect(settings.db_path)
        try:
            conn.execute(
                """INSERT INTO discover_4k_requests
                   (media_type, tmdb_id, title, requested_by, requested_by_name,
                    state, created_at)
                   VALUES ('movie', 550, 'Fight Club', 1, 'Other', 'pending', 'now')"""
            )
            conn.commit()
        finally:
            conn.close()

        # Bypass the pre-check the way a real race does: patch it to miss.
        import pensieve.api.discover_routes as dr

        original = dr._PENDING_4K_DUPLICATE
        dr._PENDING_4K_DUPLICATE = "SELECT id FROM discover_4k_requests WHERE 0 AND ? AND ?"
        try:
            resp = client.post(
                "/api/discover/request",
                json={"media_type": "movie", "tmdb_id": 550, "quality": "4K"},
            )
        finally:
            dr._PENDING_4K_DUPLICATE = original

        assert resp.status_code == 409
        assert resp.json() == {"error": "Already waiting on the owner's sign-off."}
        assert router.bodies("POST", "/api/v1/request") == []
    finally:
        client.__exit__(None, None, None)


# ------------------------------------------------------------------ browse

PAGE_BODY = {
    "page": 2,
    "totalPages": 12,
    "totalResults": 231,
    "results": [
        {
            "id": 155, "mediaType": "movie", "title": "The Dark Knight",
            "releaseDate": "2008-07-16", "posterPath": "/qJ.jpg",
            "overview": "Batman raises the stakes.", "voteAverage": 8.5,
        }
    ],
}

_PAGE_ROUTES = {("GET", "/api/v1/discover/movies"): httpx.Response(200, json=PAGE_BODY)}
_TRENDING_ROUTES = {("GET", "/api/v1/discover/trending"): httpx.Response(200, json=PAGE_BODY)}
_GENRE_ROUTES = {
    ("GET", "/api/v1/genres/tv"): httpx.Response(
        200, json=[{"id": 28, "name": "Action"}, {"id": 27, "name": "Horror"}]
    )
}


def test_browse_returns_shaped_cards_and_pagination(tmp_path):
    client, settings, _router = _make_client(tmp_path, _PAGE_ROUTES)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/browse", params={"sort": "popular", "media": "movie", "page": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 2
        assert body["total_pages"] == 12
        assert body["has_more"] is True
        assert body["items"][0]["title"] == "The Dark Knight"
        assert body["items"][0]["tmdb_id"] == 155
    finally:
        client.__exit__(None, None, None)


def test_browse_has_more_is_false_on_the_last_page(tmp_path):
    last = {**PAGE_BODY, "page": 12, "totalPages": 12}
    routes = {("GET", "/api/v1/discover/movies"): httpx.Response(200, json=last)}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        assert client.get("/api/discover/browse", params={"page": 12}).json()["has_more"] is False
    finally:
        client.__exit__(None, None, None)


def test_browse_sends_only_whitelisted_sort_keys_upstream(tmp_path):
    """The client never chooses the sortBy string; this route does."""
    client, settings, router = _make_client(tmp_path, _PAGE_ROUTES)
    try:
        _login(client, settings)
        client.get("/api/discover/browse", params={"sort": "top_rated", "media": "movie"})
        sent = router.requests[-1].url
        assert sent.params["sortBy"] == "vote_average.desc"
        assert sent.params["voteCountGte"] == "300"
    finally:
        client.__exit__(None, None, None)


def test_browse_rejects_an_unknown_sort(tmp_path):
    """Jellyseerr would answer 200 with default ordering; we answer 422."""
    client, settings, router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        assert client.get("/api/discover/browse", params={"sort": "bogus"}).status_code == 422
        assert router.requests == []
    finally:
        client.__exit__(None, None, None)


def test_browse_rejects_a_page_past_the_upstream_ceiling(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        assert client.get("/api/discover/browse", params={"page": 501}).status_code == 422
        assert client.get("/api/discover/browse", params={"page": 0}).status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_browse_trending_takes_no_filters(tmp_path):
    client, settings, _router = _make_client(tmp_path, _TRENDING_ROUTES)
    try:
        _login(client, settings)
        assert client.get("/api/discover/browse", params={"sort": "trending"}).status_code == 200
        conflict = client.get("/api/discover/browse", params={"sort": "trending", "genre": 27})
        assert conflict.status_code == 400
        assert "error" in conflict.json()
    finally:
        client.__exit__(None, None, None)


def test_browse_relays_an_upstream_outage_as_the_house_502(tmp_path):
    routes = {("GET", "/api/v1/discover/movies"): httpx.Response(500)}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/browse")
        assert resp.status_code == 502
        assert resp.json() == {"error": "jellyseerr unreachable"}
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("rating", [7, 8])
def test_browse_min_rating_passes_through_with_the_vote_floor(tmp_path, rating):
    """Route-layer regression: query params arrive as strings and Pydantic v2
    won't coerce a string into an int Literal, so a Literal[7, 8] annotation
    here rejected every value it was meant to accept. Covered at HTTP level,
    not just at the mapper, since that's exactly the layer that broke.
    """
    client, settings, router = _make_client(tmp_path, _PAGE_ROUTES)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/browse", params={"min_rating": rating})
        assert resp.status_code == 200
        sent = router.requests[-1].url.params
        assert sent["voteAverageGte"] == str(rating)
        assert sent["voteCountGte"] == "300"
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("rating", [6, 9])
def test_browse_min_rating_rejects_outside_the_closed_range(tmp_path, rating):
    client, settings, router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        resp = client.get("/api/discover/browse", params={"min_rating": rating})
        assert resp.status_code == 422
        assert router.requests == []
    finally:
        client.__exit__(None, None, None)


def test_browse_decade_sends_movie_date_bounds(tmp_path):
    client, settings, router = _make_client(tmp_path, _PAGE_ROUTES)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/browse", params={"decade": "2010s"})
        assert resp.status_code == 200
        sent = router.requests[-1].url.params
        assert sent["primaryReleaseDateGte"] == "2010-01-01"
        assert sent["primaryReleaseDateLte"] == "2019-12-31"
    finally:
        client.__exit__(None, None, None)


def test_browse_decade_with_tv_media_uses_first_air_date_bounds(tmp_path):
    routes = {("GET", "/api/v1/discover/tv"): httpx.Response(200, json=PAGE_BODY)}
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.get(
            "/api/discover/browse", params={"decade": "2010s", "media": "tv"}
        )
        assert resp.status_code == 200
        sent = router.requests[-1].url.params
        assert sent["firstAirDateGte"] == "2010-01-01"
        assert sent["firstAirDateLte"] == "2019-12-31"
        assert "primaryReleaseDateGte" not in sent
    finally:
        client.__exit__(None, None, None)


def test_browse_and_genres_require_a_session(tmp_path):
    """No _login call: both are member surfaces behind the session gate."""
    client, settings, _router = _make_client(tmp_path, {})
    try:
        assert client.get("/api/discover/browse").status_code == 401
        assert client.get("/api/discover/genres/movie").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_genres_returns_the_list_for_a_media_type(tmp_path):
    client, settings, _router = _make_client(tmp_path, _GENRE_ROUTES)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/genres/tv")
        assert resp.status_code == 200
        assert resp.json() == {
            "genres": [{"id": 28, "name": "Action"}, {"id": 27, "name": "Horror"}]
        }
    finally:
        client.__exit__(None, None, None)


def test_genres_rejects_an_unknown_media_type(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        assert client.get("/api/discover/genres/people").status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_owner_name_reaches_the_copy_a_friend_reads(tmp_path):
    """OWNER_NAME is not decoration: it is in the two messages that name a person.

    Both are the "we cannot proceed" answers on the request path, and both
    are written pronoun-free so any name substitutes cleanly.
    """
    routes = {
        **_BROWSE_ROUTES,
        ("GET", "/api/v1/user"): httpx.Response(200, json={"results": []}),
        ("POST", "/api/v1/user/import-from-plex"): httpx.Response(201, json=[]),
    }
    client, settings, _router = _make_client(tmp_path, routes, owner_name="Ada")
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request", json={"media_type": "movie", "tmdb_id": 550}
        )
        assert resp.json() == {"error": "couldn't map your account — ask Ada"}
    finally:
        client.__exit__(None, None, None)


def test_request_records_a_poster_hint_alongside_the_title(tmp_path):
    """The Pipeline board is poster-led, and a fresh request is in no arr library."""
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/discover/request", json={"media_type": "movie", "tmdb_id": 550}
        )
        assert resp.status_code == 201

        conn = connect(settings.db_path)
        try:
            hint = conn.execute("SELECT * FROM title_hints").fetchone()
        finally:
            conn.close()
        assert hint["title"] == "Fight Club"
        assert hint["poster"] == "/jSziioSwPVrOy9Yow3XhWIBDjq1.jpg"
    finally:
        client.__exit__(None, None, None)


# --- suggest -----------------------------------------------------------------


def test_suggest_200_keeps_people_the_grid_drops(tmp_path):
    """The whole point of the second route: /search cannot answer "who".

    The person row is upstream\'s third result and stays third -- the dropdown
    is a relevance list, and re-ranking it here would put "Aurora Matrix"
    above "The Matrix".
    """
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/suggest", params={"q": "matrix"})
        assert resp.status_code == 200

        items = resp.json()["items"]
        assert [row["media_type"] for row in items] == ["movie", "tv", "person"]
        assert items[2] == {
            "person_id": 4381244,
            "name": "Aurora Matrix",
            "profile_path": "/l5ho43eQhxEEz7MEavjsieA7S1W.jpg",
            "media_type": "person",
        }
    finally:
        client.__exit__(None, None, None)


def test_suggest_takes_at_most_eight(tmp_path):
    """A dropdown, not a results page: the bound is the route\'s, not the UI\'s."""
    many = {"results": [dict(SEARCH["results"][0], id=i) for i in range(40)]}
    routes = {**_BROWSE_ROUTES, ("GET", "/api/v1/search"): httpx.Response(200, json=many)}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        assert len(client.get("/api/discover/suggest", params={"q": "a"}).json()["items"]) == 8
    finally:
        client.__exit__(None, None, None)


def test_suggest_does_not_change_what_the_results_grid_gets(tmp_path):
    """/search stays title-only. Both routes read the same upstream body, and
    a person leaking into the grid would render as an unrequestable card."""
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        client.get("/api/discover/suggest", params={"q": "matrix"})
        items = client.get("/api/discover/search", params={"q": "matrix"}).json()["items"]
        assert [row["title"] for row in items] == ["The Matrix", "Threat Matrix"]
        assert all(row["media_type"] in ("movie", "tv") for row in items)
    finally:
        client.__exit__(None, None, None)


def test_suggest_and_search_cost_one_upstream_call_between_them(tmp_path):
    """Typing fires both routes; Jellyseerr should only hear about it once."""
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        client.get("/api/discover/suggest", params={"q": "matrix"})
        client.get("/api/discover/search", params={"q": "matrix"})
        sent = [r for r in router.requests if r.url.path == "/api/v1/search"]
        assert len(sent) == 1
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("q", ["", "x" * 101])
def test_suggest_422_on_an_out_of_bounds_query(tmp_path, q):
    """Same bound as /search: this is a client string reaching an upstream call."""
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        assert client.get("/api/discover/suggest", params={"q": q}).status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_suggest_502_when_jellyseerr_is_down(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        resp = client.get("/api/discover/suggest", params={"q": "matrix"})
        assert resp.status_code == 502
        assert resp.json() == {"error": "jellyseerr unreachable"}
    finally:
        client.__exit__(None, None, None)


# --- person ------------------------------------------------------------------


def test_person_200_returns_the_name_and_acting_credits(tmp_path):
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/person/31")
        assert resp.status_code == 200

        body = resp.json()
        assert body["name"] == "Tom Hanks"
        assert [item["title"] for item in body["items"]] == [
            "Forrest Gump", "Toy Story", "Saving Private Ryan",
            "The Daily Show", "Family Ties",
        ]
        # Shaped exactly like every other Discover card, so the same tile and
        # the same detail sheet work without a second code path.
        assert set(body["items"][0]) == {
            "tmdb_id", "title", "year", "media_type", "poster_path",
            "overview", "rating", "status", "availability",
        }
    finally:
        client.__exit__(None, None, None)


def test_person_credits_are_requestable_through_the_normal_sheet(tmp_path):
    """A filmography tile has to reach the same request lane as a search tile."""
    client, settings, _router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        body = client.get("/api/discover/person/31").json()
        by_title = {item["title"]: item for item in body["items"]}
        assert by_title["Saving Private Ryan"]["availability"] == "available"
        assert by_title["Forrest Gump"]["availability"] == "requestable"
    finally:
        client.__exit__(None, None, None)


def test_person_404_when_jellyseerr_has_no_such_person(tmp_path):
    """Distinct from a 502, same as the title detail route: "no such actor" and
    "the service is down" are different things to tell someone.

    Note this branch is unreachable against Jellyseerr 2.x as deployed: it
    answers an unknown person id with a **500** (``{"message": "Unable to
    retrieve person."}``), not a 404 -- verified live 2026-08-18 -- so in
    practice an unknown id takes the 502 path below. The mapping is kept
    because it is the correct one the day upstream sends the right status,
    and because deliberately reading a 500 as "no such person" would make a
    genuine Jellyseerr fault read as an empty actor.
    """
    routes = {
        **_BROWSE_ROUTES,
        ("GET", "/api/v1/person/99"): httpx.Response(404, json={"message": "Not found"}),
        ("GET", "/api/v1/person/99/combined_credits"): httpx.Response(
            200, json={"cast": [], "crew": [], "id": 99}
        ),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/person/99")
        assert resp.status_code == 404
        assert resp.json() == {"error": "not found"}
    finally:
        client.__exit__(None, None, None)


def test_person_502_when_jellyseerr_is_down(tmp_path):
    """An outage is a 5xx, and here it has to be spelled out: the empty-router
    trick the other outage tests use answers 404, which this route reads --
    correctly -- as "no such person"."""
    routes = {
        ("GET", "/api/v1/person/31"): httpx.Response(503),
        ("GET", "/api/v1/person/31/combined_credits"): httpx.Response(503),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/person/31")
        assert resp.status_code == 502
        assert resp.json() == {"error": "jellyseerr unreachable"}
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("person_id", ["0", "-1", "abc"])
def test_person_422_on_an_unusable_id(tmp_path, person_id):
    """Bounded before it reaches an upstream path segment."""
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        assert client.get(f"/api/discover/person/{person_id}").status_code == 422
        assert not [r for r in router.requests if "person" in r.url.path]
    finally:
        client.__exit__(None, None, None)


def test_a_two_word_search_reaches_jellyseerr_url_encoded(tmp_path):
    """The 1.1.0 bug, end to end: this answered 502 for every multi-word query."""
    client, settings, router = _make_client(tmp_path, _BROWSE_ROUTES)
    try:
        _login(client, settings)
        assert client.get("/api/discover/search", params={"q": "tom hanks"}).status_code == 200
        sent = [r for r in router.requests if r.url.path == "/api/v1/search"]
        assert "query=tom%20hanks" in str(sent[0].url)
        assert sent[0].url.params["query"] == "tom hanks"
    finally:
        client.__exit__(None, None, None)


def test_person_502_on_the_500_jellyseerr_actually_sends_for_an_unknown_id(tmp_path):
    """What an unknown person id really does, as opposed to what it should.

    A person id only ever reaches this route from a suggestion row the backend
    itself produced, so this is a hand-typed-URL path rather than one a friend
    can stumble into.
    """
    routes = {
        ("GET", "/api/v1/person/99"): httpx.Response(
            500, json={"message": "Unable to retrieve person."}
        ),
        ("GET", "/api/v1/person/99/combined_credits"): httpx.Response(500),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.get("/api/discover/person/99")
        assert resp.status_code == 502
        # Never the upstream's own text: that is Jellyseerr's internals.
        assert resp.json() == {"error": "jellyseerr unreachable"}
    finally:
        client.__exit__(None, None, None)
