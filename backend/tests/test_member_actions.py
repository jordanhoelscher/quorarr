"""Tests for member actions: deletion flags/vetoes and quality requests."""
import json
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from pensieve.auth import SESSION_COOKIE, sign_session
from pensieve.clients.base import CachedHTTP
from pensieve.db import connect
from pensieve.main import create_app
from tests.conftest import make_settings, seed_user


class _Router:
    """Records every outgoing request; returns canned responses by (method, path)."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.routes.get((request.method, request.url.path), httpx.Response(404))


def _make_client(tmp_path, routes: dict, **overrides) -> tuple[TestClient, object, _Router]:
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    router = _Router(routes)
    app.state.http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(router)))
    return client, settings, router


def _login(client: TestClient, settings, *, user_id: int = 2, name: str = "Sam", role: str = "member") -> None:
    # current_user is DB-authoritative (role + revocation come from the users
    # row), so a faked session needs the row to exist, not just the cookie.
    seed_user(settings, user_id=user_id, name=name, role=role)
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": user_id, "name": name, "role": role})
    )


def _insert_expired_flag(settings, *, flagged_at: datetime) -> None:
    """Insert a 'flagged' row directly via SQL, bypassing create_flag's 'now'."""
    conn = connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO deletion_flags (media_type, arr_id, season_number, title,"
            " size_bytes, reason, state, flagged_by, flagged_by_name, flagged_at)"
            " VALUES ('movie', 555, NULL, 'Old Movie', 1000, NULL, 'flagged', 2, 'Sam', ?)",
            (flagged_at.isoformat(),),
        )
    finally:
        conn.close()


# --- Deletion flags / vetoes -------------------------------------------------


_MOVIE_42 = {"id": 42, "title": "Old Movie", "qualityProfileId": 6, "sizeOnDisk": 5_000}
_FLAG_ROUTES = {("GET", "/api/v3/movie/42"): httpx.Response(200, json=_MOVIE_42)}


def test_post_flag_create_201(tmp_path):
    client, settings, _router = _make_client(tmp_path, _FLAG_ROUTES)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/flags",
            json={"media_type": "movie", "arr_id": 42, "title": "Old Movie", "size_bytes": 5000},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["state"] == "flagged"
        assert body["flagged_by_name"] == "Sam"
        assert body["title"] == "Old Movie"
    finally:
        client.__exit__(None, None, None)


def test_post_flag_duplicate_409(tmp_path):
    client, settings, _router = _make_client(tmp_path, _FLAG_ROUTES)
    try:
        _login(client, settings)
        body = {"media_type": "movie", "arr_id": 42, "title": "Old Movie", "size_bytes": 5000}
        first = client.post("/api/flags", json=body)
        assert first.status_code == 201
        second = client.post("/api/flags", json=body)
        assert second.status_code == 409
        assert "error" in second.json()
    finally:
        client.__exit__(None, None, None)


def test_post_flag_veto_200_then_409(tmp_path):
    client, settings, _router = _make_client(tmp_path, _FLAG_ROUTES)
    try:
        _login(client, settings)
        created = client.post(
            "/api/flags",
            json={"media_type": "movie", "arr_id": 42, "title": "Old Movie", "size_bytes": 5000},
        ).json()
        flag_id = created["id"]

        veto = client.post(f"/api/flags/{flag_id}/veto")
        assert veto.status_code == 200
        assert veto.json()["state"] == "vetoed"
        assert veto.json()["vetoed_by_name"] == "Sam"

        veto_again = client.post(f"/api/flags/{flag_id}/veto")
        assert veto_again.status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_get_flags_sweeps_expired_and_fires_webhook(tmp_path):
    client, settings, router = _make_client(
        tmp_path, {}, discord_webhook_url="http://discord.test/hook"
    )
    try:
        _login(client, settings)
        old = datetime.now(timezone.utc) - timedelta(days=20)
        _insert_expired_flag(settings, flagged_at=old)

        resp = client.get("/api/flags")
        assert resp.status_code == 200
        body = resp.json()
        assert body["active"] == []
        assert len(body["recent"]) == 1
        assert body["recent"][0]["title"] == "Old Movie"
        assert body["recent"][0]["state"] == "pending_approval"

        webhook_requests = [r for r in router.requests if r.url.path == "/hook"]
        assert len(webhook_requests) == 1
        sent = json.loads(webhook_requests[0].content)
        assert "Old Movie" in sent["content"]
        assert "Sam" in sent["content"]
        assert "14 days" in sent["content"]
        assert sent["allowed_mentions"] == {"parse": []}
    finally:
        client.__exit__(None, None, None)


def test_post_flags_unauthenticated_401(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        resp = client.post(
            "/api/flags",
            json={"media_type": "movie", "arr_id": 42, "title": "Old Movie", "size_bytes": 5000},
        )
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


# --- Quality requests ---------------------------------------------------------

# make_settings: radarr hd=6/4k=7, sonarr hd=4/4k=5. Profile 3 (radarr) and
# profile 2 (sonarr) stand in for "some lower profile" -- deliberately NOT the
# 4K ids, since a 1080p request against a 4K profile is a downgrade and is
# refused outright.
_MOVIE_LOW_PROFILE = {
    "id": 102, "title": "Dune", "qualityProfileId": 3,
    "sizeOnDisk": 5_000, "movieFile": {"size": 4_800},
}
_MOVIE_HD_PROFILE = {
    "id": 101, "title": "Arrival", "qualityProfileId": 6, "sizeOnDisk": 2_000,
}
_MOVIE_4K_PROFILE = {
    "id": 103, "title": "Blade Runner 2049", "qualityProfileId": 7, "sizeOnDisk": 60_000,
}
_SERIES_LOW_PROFILE = {
    "id": 202, "title": "Doctor Who", "qualityProfileId": 2,
    "statistics": {"sizeOnDisk": 9_000},
    "seasons": [
        {"seasonNumber": 3, "monitored": True, "statistics": {"sizeOnDisk": 3_000}},
    ],
}
_SERIES_4K_PROFILE = {
    "id": 203, "title": "Planet Earth", "qualityProfileId": 5,
    "statistics": {"sizeOnDisk": 40_000}, "seasons": [],
}


def _quality_routes(**extra: httpx.Response) -> dict[tuple[str, str], httpx.Response]:
    routes: dict[tuple[str, str], httpx.Response] = {}
    routes.update(extra)
    return routes


def test_quality_request_1080p_low_profile_movie_switches_and_searches(tmp_path):
    routes = {
        ("GET", "/api/v3/movie/102"): httpx.Response(200, json=_MOVIE_LOW_PROFILE),
        ("PUT", "/api/v3/movie/102"): httpx.Response(200, json=_MOVIE_LOW_PROFILE),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 102, "title": "Dune", "requested": "1080p"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "auto_triggered"

        # GET (route's own profile lookup), GET (set_profile's internal
        # GET-then-PUT round-trip), PUT, POST (search command).
        methods = [r.method for r in router.requests]
        assert methods == ["GET", "GET", "PUT", "POST"]
        put_body = json.loads(router.requests[2].content)
        assert put_body["qualityProfileId"] == 6
        command_body = json.loads(router.requests[3].content)
        assert command_body == {"name": "MoviesSearch", "movieIds": [102]}

        rows = client.get("/api/quality-requests").json()["items"]
        assert rows[0]["state"] == "auto_triggered"
        assert rows[0]["title"] == "Dune"
    finally:
        client.__exit__(None, None, None)


def test_quality_request_1080p_already_hd_profile_only_searches(tmp_path):
    routes = {
        ("GET", "/api/v3/movie/101"): httpx.Response(200, json=_MOVIE_HD_PROFILE),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 101, "title": "Arrival", "requested": "1080p"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "auto_triggered"

        methods = [r.method for r in router.requests]
        assert methods == ["GET", "POST"]  # no PUT -- already on the HD profile
    finally:
        client.__exit__(None, None, None)


def test_quality_request_4k_needs_approval_no_arr_calls_and_fires_webhook(tmp_path):
    routes = {
        ("GET", "/api/v3/movie/102"): httpx.Response(200, json=_MOVIE_LOW_PROFILE),
    }
    client, settings, router = _make_client(
        tmp_path, routes, discord_webhook_url="http://discord.test/hook"
    )
    try:
        _login(client, settings)
        resp = client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 102, "title": "Dune", "requested": "4K"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "pending_approval"

        methods = [r.method for r in router.requests]
        assert methods == ["GET", "POST"]  # GET the profile, POST is the webhook only
        assert router.requests[1].url.path == "/hook"
        sent = json.loads(router.requests[1].content)
        assert "Dune" in sent["content"]
        assert "Sam" in sent["content"]
        assert sent["allowed_mentions"] == {"parse": []}

        rows = client.get("/api/quality-requests").json()["items"]
        assert rows[0]["state"] == "pending_approval"
    finally:
        client.__exit__(None, None, None)


def test_quality_request_radarr_500_during_auto_marks_error_and_502(tmp_path):
    routes = {
        ("GET", "/api/v3/movie/102"): httpx.Response(200, json=_MOVIE_LOW_PROFILE),
        ("PUT", "/api/v3/movie/102"): httpx.Response(500, text="boom"),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 102, "title": "Dune", "requested": "1080p"},
        )
        assert resp.status_code == 502
        body = resp.json()
        assert body["state"] == "error"
        # The HTTP response must be sanitized -- no internal hostnames/ports
        # (radarr's base URL, "http://radarr:7878", would leak them).
        assert body["error"] == "radarr unreachable"
        assert "http" not in body["error"]

        rows = client.get("/api/quality-requests").json()["items"]
        assert rows[0]["state"] == "error"
        # The member listing must not expose the raw error text either --
        # same leak, different route. "error" must be absent entirely, not
        # just falsy, so the UI can rely on the key never being present.
        assert "error" not in rows[0]

        # But the DB row keeps the full exception text for the owner's
        # Approvals view (Task 15) -- read it directly rather than through
        # the member-facing API, which never returns it.
        conn = connect(settings.db_path)
        try:
            db_row = conn.execute(
                "SELECT error FROM quality_requests WHERE id = ?", (rows[0]["id"],)
            ).fetchone()
        finally:
            conn.close()
        assert "radarr request failed" in db_row["error"]
    finally:
        client.__exit__(None, None, None)


def test_quality_request_series_with_season_number_triggers_season_search(tmp_path):
    routes = {
        ("GET", "/api/v3/series/202"): httpx.Response(200, json=_SERIES_LOW_PROFILE),
        ("PUT", "/api/v3/series/202"): httpx.Response(200, json=_SERIES_LOW_PROFILE),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/quality-requests",
            json={
                "media_type": "series", "arr_id": 202, "season_number": 3,
                "title": "Doctor Who", "requested": "1080p",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "auto_triggered"

        command_requests = [r for r in router.requests if r.url.path == "/api/v3/command"]
        assert len(command_requests) == 1
        command_body = json.loads(command_requests[0].content)
        assert command_body == {"name": "SeasonSearch", "seriesId": 202, "seasonNumber": 3}
    finally:
        client.__exit__(None, None, None)


def test_quality_request_duplicate_1080p_dedupe_within_24h(tmp_path):
    routes = {
        ("GET", "/api/v3/movie/101"): httpx.Response(200, json=_MOVIE_HD_PROFILE),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        body = {"media_type": "movie", "arr_id": 101, "title": "Arrival", "requested": "1080p"}
        first = client.post("/api/quality-requests", json=body)
        assert first.status_code == 200
        assert first.json()["state"] == "auto_triggered"

        second = client.post("/api/quality-requests", json=body)
        assert second.status_code == 409
        assert second.json() == {"error": "duplicate request"}

        # The arr search from the first (successful) request must not fire again.
        command_requests = [r for r in router.requests if r.url.path == "/api/v3/command"]
        assert len(command_requests) == 1
    finally:
        client.__exit__(None, None, None)


def test_quality_request_4k_duplicate_while_pending_409(tmp_path):
    routes = {
        ("GET", "/api/v3/movie/102"): httpx.Response(200, json=_MOVIE_LOW_PROFILE),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        body = {"media_type": "movie", "arr_id": 102, "title": "Dune", "requested": "4K"}
        first = client.post("/api/quality-requests", json=body)
        assert first.status_code == 200
        assert first.json()["state"] == "pending_approval"

        second = client.post("/api/quality-requests", json=body)
        assert second.status_code == 409
        assert second.json() == {"error": "duplicate request"}

        # Second call is rejected by the dedupe check before it ever reaches
        # the profile-lookup GET.
        get_requests = [r for r in router.requests if r.method == "GET"]
        assert len(get_requests) == 1
    finally:
        client.__exit__(None, None, None)


def test_get_quality_requests_newest_first(tmp_path):
    routes = {
        ("GET", "/api/v3/movie/101"): httpx.Response(200, json=_MOVIE_HD_PROFILE),
        ("GET", "/api/v3/movie/102"): httpx.Response(200, json=_MOVIE_LOW_PROFILE),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 101, "title": "Arrival", "requested": "4K"},
        )
        client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 102, "title": "Dune", "requested": "4K"},
        )

        rows = client.get("/api/quality-requests").json()["items"]
        assert [r["title"] for r in rows] == ["Dune", "Arrival"]
    finally:
        client.__exit__(None, None, None)


# --- Server-derived flag identity (confused-deputy fix) ------------------------


def test_post_flag_stores_arr_derived_title_not_the_client_one(tmp_path):
    """The owner approves what the *server* says ``arr_id`` is.

    A member controls ``arr_id``, ``title`` and ``size_bytes`` independently,
    but execution routes on ``arr_id`` alone -- so a mismatched title would
    have the owner consenting to delete a different file than the one named
    in the dialog and the Discord ping. Both fields are now resolved from the
    arr and the client's values are ignored.
    """
    routes = {
        ("GET", "/api/v3/movie/42"): httpx.Response(
            200,
            json={"id": 42, "title": "Blade Runner 2049", "qualityProfileId": 7,
                  "sizeOnDisk": 61_000, "movieFile": {"size": 60_000}},
        )
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/flags",
            json={
                "media_type": "movie", "arr_id": 42,
                "title": "Some Junk Nobody Watches (0 plays)", "size_bytes": 1,
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Blade Runner 2049"
        assert body["size_bytes"] == 60_000
    finally:
        client.__exit__(None, None, None)


def test_post_flag_unknown_arr_id_404(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})  # every arr GET 404s
    try:
        _login(client, settings)
        resp = client.post(
            "/api/flags",
            json={"media_type": "movie", "arr_id": 4242, "title": "Ghost", "size_bytes": 5},
        )
        assert resp.status_code == 404
        assert resp.json() == {"error": "not found"}
    finally:
        client.__exit__(None, None, None)


def test_post_flag_series_season_uses_that_seasons_size(tmp_path):
    routes = {("GET", "/api/v3/series/202"): httpx.Response(200, json=_SERIES_LOW_PROFILE)}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/flags",
            json={"media_type": "series", "arr_id": 202, "season_number": 3,
                  "title": "wrong", "size_bytes": 1},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Doctor Who"
        assert body["size_bytes"] == 3_000  # the season's, not the series' 9_000
    finally:
        client.__exit__(None, None, None)


def test_post_flag_bogus_season_404(tmp_path):
    routes = {("GET", "/api/v3/series/202"): httpx.Response(200, json=_SERIES_LOW_PROFILE)}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/flags",
            json={"media_type": "series", "arr_id": 202, "season_number": 99,
                  "title": "Doctor Who", "size_bytes": 1},
        )
        assert resp.status_code == 404
        assert resp.json() == {"error": "season not found"}
    finally:
        client.__exit__(None, None, None)


def test_post_flag_arr_unreachable_is_502_not_404(tmp_path):
    """An arr being down must not read as "that item doesn't exist"."""
    routes = {("GET", "/api/v3/movie/42"): httpx.Response(500, text="boom")}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/flags",
            json={"media_type": "movie", "arr_id": 42, "title": "Old Movie", "size_bytes": 5},
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "radarr unreachable"}
    finally:
        client.__exit__(None, None, None)


def test_quality_request_stores_arr_derived_title(tmp_path):
    routes = {
        ("GET", "/api/v3/movie/101"): httpx.Response(200, json=_MOVIE_HD_PROFILE),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 101, "title": "Not This", "requested": "1080p"},
        )
        assert resp.status_code == 200
        rows = client.get("/api/quality-requests").json()["items"]
        assert rows[0]["title"] == "Arrival"
    finally:
        client.__exit__(None, None, None)


def test_quality_request_unknown_arr_id_404(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        resp = client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 4242, "title": "Ghost", "requested": "1080p"},
        )
        assert resp.status_code == 404
        assert resp.json() == {"error": "not found"}
    finally:
        client.__exit__(None, None, None)


def test_quality_request_bogus_season_404(tmp_path):
    routes = {("GET", "/api/v3/series/202"): httpx.Response(200, json=_SERIES_LOW_PROFILE)}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/quality-requests",
            json={"media_type": "series", "arr_id": 202, "season_number": 99,
                  "title": "Doctor Who", "requested": "1080p"},
        )
        assert resp.status_code == 404
        assert resp.json() == {"error": "season not found"}
    finally:
        client.__exit__(None, None, None)


# --- Downgrade gate ------------------------------------------------------------


def test_quality_request_1080p_on_4k_movie_is_409(tmp_path):
    """A 1080p request against a 4K item is a *downgrade*, and auto-executes.

    Left ungated it switches the profile down and fires a search, which can
    replace the 4K file on import -- irreversible loss, no owner in the loop.
    """
    routes = {("GET", "/api/v3/movie/103"): httpx.Response(200, json=_MOVIE_4K_PROFILE)}
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 103, "title": "Blade Runner 2049",
                  "requested": "1080p"},
        )
        assert resp.status_code == 409
        assert resp.json() == {"error": "already 4K — downgrades aren't supported"}

        # No profile switch, no search, and no row to retry later.
        assert [r.method for r in router.requests] == ["GET"]
        assert client.get("/api/quality-requests").json()["items"] == []
    finally:
        client.__exit__(None, None, None)


def test_quality_request_1080p_on_4k_series_is_409(tmp_path):
    routes = {("GET", "/api/v3/series/203"): httpx.Response(200, json=_SERIES_4K_PROFILE)}
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        resp = client.post(
            "/api/quality-requests",
            json={"media_type": "series", "arr_id": 203, "title": "Planet Earth",
                  "requested": "1080p"},
        )
        assert resp.status_code == 409
        assert resp.json() == {"error": "already 4K — downgrades aren't supported"}
        assert [r.method for r in router.requests] == ["GET"]
    finally:
        client.__exit__(None, None, None)


# --- Member-facing leakage -----------------------------------------------------


def test_member_flag_listing_omits_the_error_column(tmp_path):
    """``error`` holds raw upstream text (internal hostnames/ports); owner-only.

    ``approved`` is in the member-visible ``recent`` list, so an
    approved-with-error row is exactly what a friend would see.
    """
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        conn = connect(settings.db_path)
        try:
            conn.execute(
                "INSERT INTO deletion_flags (media_type, arr_id, season_number, title,"
                " size_bytes, reason, state, flagged_by, flagged_by_name, flagged_at, error)"
                " VALUES ('movie', 42, NULL, 'Old Movie', 1000, NULL, 'approved', 2, 'Sam', ?,"
                " 'radarr request failed: 500 for url http://radarr:7878/api/v3/movie/42')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.execute(
                "INSERT INTO deletion_flags (media_type, arr_id, season_number, title,"
                " size_bytes, reason, state, flagged_by, flagged_by_name, flagged_at, error)"
                " VALUES ('movie', 43, NULL, 'Newer Movie', 1000, NULL, 'flagged', 2, 'Sam', ?,"
                " 'radarr request failed: http://radarr:7878')",
                (datetime.now(timezone.utc).isoformat(),),
            )
        finally:
            conn.close()

        body = client.get("/api/flags").json()
        rows = body["active"] + body["recent"]
        assert len(rows) == 2
        for row in rows:
            assert "error" not in row
    finally:
        client.__exit__(None, None, None)


# --- Input bounds --------------------------------------------------------------


def test_oversized_free_text_is_rejected_422(tmp_path):
    client, settings, _router = _make_client(tmp_path, _FLAG_ROUTES)
    try:
        _login(client, settings)
        too_long = "x" * 1001
        flag = client.post(
            "/api/flags",
            json={"media_type": "movie", "arr_id": 42, "reason": too_long},
        )
        assert flag.status_code == 422

        quality = client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 42, "requested": "1080p",
                  "current_quality": "y" * 101},
        )
        assert quality.status_code == 422

        title = client.post(
            "/api/flags",
            json={"media_type": "movie", "arr_id": 42, "title": "t" * 301},
        )
        assert title.status_code == 422
    finally:
        client.__exit__(None, None, None)
