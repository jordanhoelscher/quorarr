"""Tests for owner admin routes (approval queue, execution) and the hourly sweep."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from pensieve.auth import SESSION_COOKIE, sign_session
from pensieve.clients.base import CachedHTTP
from pensieve.db import connect, init_db
from pensieve.main import create_app, run_sweep_once
from pensieve.services.deletion import FlagError
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


def _login(client: TestClient, settings, *, user_id: int = 1, name: str = "Ada", role: str = "owner") -> None:
    # current_user is DB-authoritative (role + revocation come from the users
    # row), so a faked session needs the row to exist, not just the cookie.
    seed_user(settings, user_id=user_id, name=name, role=role)
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": user_id, "name": name, "role": role})
    )


def _insert_flag(
    settings, *, state: str = "pending_approval", media_type: str = "movie", arr_id: int = 42,
    season_number: int | None = None, title: str = "Old Movie",
    flagged_at: datetime | None = None,
) -> int:
    """Insert a deletion_flags row directly, bypassing the state machine."""
    flagged_at = flagged_at or datetime.now(timezone.utc)
    conn = connect(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO deletion_flags (media_type, arr_id, season_number, title,"
            " size_bytes, reason, state, flagged_by, flagged_by_name, flagged_at)"
            " VALUES (?, ?, ?, ?, 1000, NULL, ?, 2, 'Sam', ?)",
            (media_type, arr_id, season_number, title, state, flagged_at.isoformat()),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _insert_quality_request(
    settings, *, state: str = "pending_approval", media_type: str = "movie", arr_id: int = 102,
    season_number: int | None = None, title: str = "Dune", requested: str = "4K",
    error: str | None = None,
) -> int:
    """Insert a quality_requests row directly, bypassing the member-facing route."""
    now = datetime.now(timezone.utc)
    conn = connect(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO quality_requests (media_type, arr_id, season_number, title,"
            " current_quality, requested_quality, state, requested_by, requested_by_name,"
            " created_at, error) VALUES (?, ?, ?, ?, NULL, ?, ?, 2, 'Sam', ?, ?)",
            (media_type, arr_id, season_number, title, requested, state, now.isoformat(), error),
        )
        return cur.lastrowid
    finally:
        conn.close()


# --- Role gate ----------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/admin/queue"),
        ("POST", "/api/admin/flags/1/approve"),
        ("POST", "/api/admin/flags/1/deny"),
        ("POST", "/api/admin/quality/1/approve"),
        ("POST", "/api/admin/quality/1/deny"),
        ("POST", "/api/admin/discover-4k/1/approve"),
        ("POST", "/api/admin/discover-4k/1/deny"),
    ],
)
def test_member_403_on_admin_routes(tmp_path, method, path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings, role="member")
        resp = client.request(method, path)
        assert resp.status_code == 403
    finally:
        client.__exit__(None, None, None)


# --- GET /api/admin/queue ------------------------------------------------------


def test_owner_queue_returns_pending_deletion_and_error_quality_with_detail(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        flag_id = _insert_flag(settings, state="pending_approval", title="Old Movie")
        req_id = _insert_quality_request(
            settings, state="error", title="Dune", error="radarr request failed: boom"
        )
        # An auto_triggered row must not leak into the owner queue.
        _insert_quality_request(settings, state="auto_triggered", title="Arrival", requested="1080p")

        resp = client.get("/api/admin/queue")
        assert resp.status_code == 200
        body = resp.json()

        assert [d["id"] for d in body["deletions"]] == [flag_id]
        assert body["deletions"][0]["title"] == "Old Movie"

        assert [q["id"] for q in body["quality"]] == [req_id]
        assert body["quality"][0]["title"] == "Dune"
        # This is exactly the sanitized-away detail from Task 14 -- owner-only.
        assert body["quality"][0]["error"] == "radarr request failed: boom"
    finally:
        client.__exit__(None, None, None)


# --- Deletion flag approve/deny ------------------------------------------------


def test_approve_movie_flag_deletes_via_radarr(tmp_path):
    routes = {("DELETE", "/api/v3/movie/42"): httpx.Response(200)}
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        flag_id = _insert_flag(settings, media_type="movie", arr_id=42)

        resp = client.post(f"/api/admin/flags/{flag_id}/approve")
        assert resp.status_code == 200
        assert resp.json() == {"state": "executed"}

        delete_reqs = [r for r in router.requests if r.method == "DELETE"]
        assert len(delete_reqs) == 1
        assert delete_reqs[0].url.path == "/api/v3/movie/42"
        assert delete_reqs[0].url.params["deleteFiles"] == "true"

        conn = connect(settings.db_path)
        row = conn.execute("SELECT state, error FROM deletion_flags WHERE id = ?", (flag_id,)).fetchone()
        conn.close()
        assert row["state"] == "executed"
        assert row["error"] is None
    finally:
        client.__exit__(None, None, None)


_SERIES_RAW = {
    "id": 202, "title": "Doctor Who", "qualityProfileId": 5,
    "seasons": [
        {"seasonNumber": 1, "monitored": True},
        {"seasonNumber": 2, "monitored": True},
    ],
}


def test_approve_season_flag_deletes_episode_files_and_unmonitors(tmp_path):
    routes = {
        ("GET", "/api/v3/episodefile"): httpx.Response(
            200,
            json=[
                {"id": 501, "seasonNumber": 2, "size": 100,
                 "quality": {"quality": {"name": "WEBDL-1080p", "resolution": 1080}}},
                {"id": 502, "seasonNumber": 2, "size": 200,
                 "quality": {"quality": {"name": "WEBDL-1080p", "resolution": 1080}}},
                # Season 1 file must be left alone.
                {"id": 503, "seasonNumber": 1, "size": 300,
                 "quality": {"quality": {"name": "WEBDL-1080p", "resolution": 1080}}},
            ],
        ),
        ("DELETE", "/api/v3/episodefile/501"): httpx.Response(200),
        ("DELETE", "/api/v3/episodefile/502"): httpx.Response(200),
        ("GET", "/api/v3/series/202"): httpx.Response(200, json=_SERIES_RAW),
        ("PUT", "/api/v3/series/202"): httpx.Response(200, json=_SERIES_RAW),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        flag_id = _insert_flag(
            settings, media_type="series", arr_id=202, season_number=2, title="Doctor Who"
        )

        resp = client.post(f"/api/admin/flags/{flag_id}/approve")
        assert resp.status_code == 200
        assert resp.json() == {"state": "executed"}

        delete_reqs = sorted(r.url.path for r in router.requests if r.method == "DELETE")
        assert delete_reqs == ["/api/v3/episodefile/501", "/api/v3/episodefile/502"]

        put_reqs = [r for r in router.requests if r.method == "PUT"]
        assert len(put_reqs) == 1
        put_body = json.loads(put_reqs[0].content)
        season_2 = next(s for s in put_body["seasons"] if s["seasonNumber"] == 2)
        season_1 = next(s for s in put_body["seasons"] if s["seasonNumber"] == 1)
        assert season_2["monitored"] is False
        assert season_1["monitored"] is True  # untouched
    finally:
        client.__exit__(None, None, None)


def test_approve_flag_retries_after_arr_failure(tmp_path):
    routes = {("DELETE", "/api/v3/movie/42"): httpx.Response(500, text="boom")}
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        flag_id = _insert_flag(settings, media_type="movie", arr_id=42)

        first = client.post(f"/api/admin/flags/{flag_id}/approve")
        assert first.status_code == 502
        body = first.json()
        assert body["state"] == "approved"
        assert body["error"] == "radarr unreachable"
        assert "http" not in body["error"]

        conn = connect(settings.db_path)
        row = conn.execute("SELECT state, error FROM deletion_flags WHERE id = ?", (flag_id,)).fetchone()
        conn.close()
        assert row["state"] == "approved"
        assert "radarr request failed" in row["error"]

        # A second approve call on a state == 'approved' row must not try to
        # resolve_flag again (that would raise -- only 'pending_approval' is
        # a valid source state) and must instead retry execution directly.
        router.routes[("DELETE", "/api/v3/movie/42")] = httpx.Response(200)
        second = client.post(f"/api/admin/flags/{flag_id}/approve")
        assert second.status_code == 200
        assert second.json() == {"state": "executed"}

        conn = connect(settings.db_path)
        row = conn.execute("SELECT state, error FROM deletion_flags WHERE id = ?", (flag_id,)).fetchone()
        conn.close()
        assert row["state"] == "executed"
        assert row["error"] is None
    finally:
        client.__exit__(None, None, None)


def test_approve_flag_not_found_404(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        resp = client.post("/api/admin/flags/999/approve")
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_approve_flag_concurrent_execution_race_returns_executed_not_500(tmp_path, monkeypatch):
    """A second, racing approve call must not 500 if the flag was executed
    out from under it.

    Simulates two approve calls racing after the same UpstreamError: the
    first to reach ``mark_error`` wins and marks the row ``executed``: the
    second's ``mark_error`` call then hits a precondition failure
    (``mark_error`` only accepts source state ``'approved'``) and must raise
    ``FlagError`` rather than silently corrupting state. The route must
    catch that, re-check the row, and recognize the flag already finished --
    not propagate an unhandled 500. The fake here writes the 'executed' row
    itself (mimicking the winning racer) before raising, so the route's
    post-except re-check sees exactly what a real race would produce.
    """
    from pensieve.api import admin_routes

    routes = {("DELETE", "/api/v3/movie/42"): httpx.Response(500, text="boom")}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        flag_id = _insert_flag(settings, media_type="movie", arr_id=42)

        def fake_mark_error(conn, fid, error):
            conn.execute(
                "UPDATE deletion_flags SET state = 'executed', error = NULL WHERE id = ?",
                (fid,),
            )
            raise FlagError(f"flag {fid} is in state 'executed', not 'approved'")

        monkeypatch.setattr(admin_routes.deletion, "mark_error", fake_mark_error)

        resp = client.post(f"/api/admin/flags/{flag_id}/approve")
        assert resp.status_code == 200
        assert resp.json() == {"state": "executed"}

        conn = connect(settings.db_path)
        row = conn.execute("SELECT state FROM deletion_flags WHERE id = ?", (flag_id,)).fetchone()
        conn.close()
        assert row["state"] == "executed"
    finally:
        client.__exit__(None, None, None)


def test_approve_flag_concurrent_race_non_executed_state_falls_back_to_502(tmp_path, monkeypatch):
    """If mark_error's precondition fails but the row isn't 'executed'
    (e.g. denied by another admin mid-flight), fall back to the normal 502
    sanitized response rather than crashing -- and without writing anything
    (mark_error raises before its own write in this branch).
    """
    from pensieve.api import admin_routes

    routes = {("DELETE", "/api/v3/movie/42"): httpx.Response(500, text="boom")}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        flag_id = _insert_flag(settings, media_type="movie", arr_id=42)

        def fake_mark_error(conn, fid, error):
            raise FlagError(f"flag {fid} is in state 'denied', not 'approved'")

        monkeypatch.setattr(admin_routes.deletion, "mark_error", fake_mark_error)

        resp = client.post(f"/api/admin/flags/{flag_id}/approve")
        assert resp.status_code == 502
        body = resp.json()
        assert body["state"] == "approved"
        assert body["error"] == "radarr unreachable"
    finally:
        client.__exit__(None, None, None)


def test_deny_flag_stores_note(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        flag_id = _insert_flag(settings)

        resp = client.post(f"/api/admin/flags/{flag_id}/deny", json={"note": "keeping this one"})
        assert resp.status_code == 200
        assert resp.json() == {"state": "denied"}

        conn = connect(settings.db_path)
        row = conn.execute("SELECT state, note FROM deletion_flags WHERE id = ?", (flag_id,)).fetchone()
        conn.close()
        assert row["state"] == "denied"
        assert row["note"] == "keeping this one"
    finally:
        client.__exit__(None, None, None)


def test_deny_flag_wrong_state_409(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        flag_id = _insert_flag(settings, state="flagged")

        resp = client.post(f"/api/admin/flags/{flag_id}/deny")
        assert resp.status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_deny_flag_not_found_404(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        resp = client.post("/api/admin/flags/999/deny")
        assert resp.status_code == 404
    finally:
        client.__exit__(None, None, None)


# --- Quality request approve/deny ----------------------------------------------


def test_quality_approve_movie_switches_profile_and_searches(tmp_path):
    movie_raw = {"id": 102, "title": "Dune", "qualityProfileId": 6}
    routes = {
        ("GET", "/api/v3/movie/102"): httpx.Response(200, json=movie_raw),
        ("PUT", "/api/v3/movie/102"): httpx.Response(200, json=movie_raw),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_quality_request(settings, media_type="movie", arr_id=102, requested="4K")

        resp = client.post(f"/api/admin/quality/{req_id}/approve")
        assert resp.status_code == 200
        assert resp.json() == {"state": "approved"}

        put_reqs = [r for r in router.requests if r.method == "PUT"]
        assert len(put_reqs) == 1
        put_body = json.loads(put_reqs[0].content)
        assert put_body["qualityProfileId"] == 7  # radarr_profile_4k_id, see conftest.make_settings

        command_reqs = [r for r in router.requests if r.url.path == "/api/v3/command"]
        assert len(command_reqs) == 1
        assert json.loads(command_reqs[0].content) == {"name": "MoviesSearch", "movieIds": [102]}

        conn = connect(settings.db_path)
        row = conn.execute("SELECT state, resolved_at FROM quality_requests WHERE id = ?", (req_id,)).fetchone()
        conn.close()
        assert row["state"] == "approved"
        assert row["resolved_at"] is not None
    finally:
        client.__exit__(None, None, None)


def test_quality_approve_series_season_scoped_search(tmp_path):
    series_raw = {"id": 202, "title": "Doctor Who", "qualityProfileId": 4, "seasons": []}
    routes = {
        ("GET", "/api/v3/series/202"): httpx.Response(200, json=series_raw),
        ("PUT", "/api/v3/series/202"): httpx.Response(200, json=series_raw),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_quality_request(
            settings, media_type="series", arr_id=202, season_number=3,
            title="Doctor Who", requested="4K",
        )

        resp = client.post(f"/api/admin/quality/{req_id}/approve")
        assert resp.status_code == 200

        put_body = json.loads([r for r in router.requests if r.method == "PUT"][0].content)
        assert put_body["qualityProfileId"] == 5  # sonarr_profile_4k_id

        command_body = json.loads(
            [r for r in router.requests if r.url.path == "/api/v3/command"][0].content
        )
        assert command_body == {"name": "SeasonSearch", "seriesId": 202, "seasonNumber": 3}
    finally:
        client.__exit__(None, None, None)


def test_quality_approve_arr_failure_leaves_row_in_error_state(tmp_path):
    routes = {
        ("GET", "/api/v3/movie/102"): httpx.Response(200, json={"id": 102, "qualityProfileId": 6}),
        ("PUT", "/api/v3/movie/102"): httpx.Response(500, text="boom"),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_quality_request(settings, media_type="movie", arr_id=102, requested="4K")

        resp = client.post(f"/api/admin/quality/{req_id}/approve")
        assert resp.status_code == 502
        body = resp.json()
        assert body["state"] == "error"
        assert body["error"] == "radarr unreachable"

        conn = connect(settings.db_path)
        row = conn.execute("SELECT state, error FROM quality_requests WHERE id = ?", (req_id,)).fetchone()
        conn.close()
        assert row["state"] == "error"
        assert "radarr request failed" in row["error"]
    finally:
        client.__exit__(None, None, None)


def test_quality_deny_stores_note(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        req_id = _insert_quality_request(settings)

        resp = client.post(f"/api/admin/quality/{req_id}/deny", json={"note": "too big for the NAS"})
        assert resp.status_code == 200
        assert resp.json() == {"state": "denied"}

        conn = connect(settings.db_path)
        row = conn.execute("SELECT state, note FROM quality_requests WHERE id = ?", (req_id,)).fetchone()
        conn.close()
        assert row["state"] == "denied"
        assert row["note"] == "too big for the NAS"
    finally:
        client.__exit__(None, None, None)


def test_quality_approve_auto_triggered_row_409(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        req_id = _insert_quality_request(settings, state="auto_triggered", requested="1080p")

        resp = client.post(f"/api/admin/quality/{req_id}/approve")
        assert resp.status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_quality_deny_error_row_409(tmp_path):
    """An 'error' row already made upstream calls -- deny isn't valid, only retry-approve."""
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        req_id = _insert_quality_request(settings, state="error", error="radarr request failed: boom")

        resp = client.post(f"/api/admin/quality/{req_id}/deny")
        assert resp.status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_deny_notes_are_length_bounded_422(tmp_path):
    """Deny notes render into the owner queue and (for quality) back to the
    requester -- an unbounded string is DB bloat plus layout destruction."""
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        flag_id = _insert_flag(settings)
        req_id = _insert_quality_request(settings)
        too_long = "x" * 1001

        assert client.post(
            f"/api/admin/flags/{flag_id}/deny", json={"note": too_long}
        ).status_code == 422
        assert client.post(
            f"/api/admin/quality/{req_id}/deny", json={"note": too_long}
        ).status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_quality_retry_of_errored_1080p_row_uses_the_hd_profile(tmp_path):
    """Every ``error`` row is a 1080p request, and Retry is the only button on it.

    ``error`` is written on the member path exclusively inside the
    1080p-only auto-trigger block, and the Approvals UI renders "Retry
    search" for exactly those rows. Deriving the target from ``media_type``
    alone therefore silently escalates a 1080p retry to the 4K profile and
    fires a 4K search -- while the toast says "Searching for a 1080p copy".
    """
    movie_raw = {"id": 102, "title": "Dune", "qualityProfileId": 3}
    routes = {
        ("GET", "/api/v3/movie/102"): httpx.Response(200, json=movie_raw),
        ("PUT", "/api/v3/movie/102"): httpx.Response(200, json=movie_raw),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_quality_request(
            settings, media_type="movie", arr_id=102, requested="1080p",
            state="error", error="radarr request failed: boom",
        )

        resp = client.post(f"/api/admin/quality/{req_id}/approve")
        assert resp.status_code == 200

        put_body = json.loads([r for r in router.requests if r.method == "PUT"][0].content)
        assert put_body["qualityProfileId"] == 6  # radarr_profile_hd_id, NOT 7

        command_reqs = [r for r in router.requests if r.url.path == "/api/v3/command"]
        assert len(command_reqs) == 1
        assert json.loads(command_reqs[0].content) == {"name": "MoviesSearch", "movieIds": [102]}
    finally:
        client.__exit__(None, None, None)


def test_quality_retry_of_errored_1080p_series_row_uses_the_hd_profile(tmp_path):
    series_raw = {"id": 202, "title": "Doctor Who", "qualityProfileId": 2, "seasons": []}
    routes = {
        ("GET", "/api/v3/series/202"): httpx.Response(200, json=series_raw),
        ("PUT", "/api/v3/series/202"): httpx.Response(200, json=series_raw),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_quality_request(
            settings, media_type="series", arr_id=202, title="Doctor Who",
            requested="1080p", state="error", error="sonarr request failed: boom",
        )

        assert client.post(f"/api/admin/quality/{req_id}/approve").status_code == 200
        put_body = json.loads([r for r in router.requests if r.method == "PUT"][0].content)
        assert put_body["qualityProfileId"] == 4  # sonarr_profile_hd_id, NOT 5
    finally:
        client.__exit__(None, None, None)


def test_deny_closes_out_an_approved_flag_whose_execution_keeps_failing(tmp_path):
    """An ``approved`` flag whose arr call fails forever needs a terminal exit.

    Retry is otherwise the only permitted transition, so a movie already
    removed from Radarr by hand leaves a row that 404s on every retry,
    parks in the owner's queue, shows "Approved" to every member for a title
    that was never deleted, and blocks re-flagging that scope permanently.
    """
    routes = {("DELETE", "/api/v3/movie/42"): httpx.Response(404, text="not found")}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        flag_id = _insert_flag(settings, media_type="movie", arr_id=42)

        assert client.post(f"/api/admin/flags/{flag_id}/approve").status_code == 502

        resp = client.post(
            f"/api/admin/flags/{flag_id}/deny", json={"note": "already gone from Radarr"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"state": "denied"}

        conn = connect(settings.db_path)
        row = conn.execute(
            "SELECT state, note, resolved_at FROM deletion_flags WHERE id = ?", (flag_id,)
        ).fetchone()
        conn.close()
        assert row["state"] == "denied"
        assert row["note"] == "already gone from Radarr"
        assert row["resolved_at"] is not None
    finally:
        client.__exit__(None, None, None)


# --- Hourly sweep ---------------------------------------------------------------


async def test_run_sweep_once_moves_expired_and_notifies(tmp_path):
    settings = make_settings(tmp_path, discord_webhook_url="http://discord.test/hook")
    conn = connect(settings.db_path)
    init_db(conn)
    old = datetime.now(timezone.utc) - timedelta(days=20)
    conn.execute(
        "INSERT INTO deletion_flags (media_type, arr_id, season_number, title,"
        " size_bytes, reason, state, flagged_by, flagged_by_name, flagged_at)"
        " VALUES ('movie', 555, NULL, 'Old Movie', 1000, NULL, 'flagged', 2, 'Sam', ?)",
        (old.isoformat(),),
    )
    conn.close()

    router = _Router({})
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(router)))

    moved = await run_sweep_once(settings, http)

    assert len(moved) == 1
    assert moved[0]["title"] == "Old Movie"

    webhook_reqs = [r for r in router.requests if r.url.path == "/hook"]
    assert len(webhook_reqs) == 1
    sent = json.loads(webhook_reqs[0].content)
    assert "Old Movie" in sent["content"]
    assert "Sam" in sent["content"]
    assert "14 days" in sent["content"]

    conn = connect(settings.db_path)
    row = conn.execute("SELECT state FROM deletion_flags WHERE arr_id = 555").fetchone()
    conn.close()
    assert row["state"] == "pending_approval"


async def test_run_sweep_once_no_expired_rows_is_a_noop(tmp_path):
    settings = make_settings(tmp_path, discord_webhook_url="http://discord.test/hook")
    conn = connect(settings.db_path)
    init_db(conn)
    conn.close()

    router = _Router({})
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(router)))

    moved = await run_sweep_once(settings, http)
    assert moved == []
    assert router.requests == []


def test_lifespan_starts_and_cancels_sweep_task_cleanly(tmp_path):
    """The sweep task must be cancellable on shutdown without hanging.

    It sleeps for SWEEP_INTERVAL_SECONDS (3600s) before its first tick, so if
    cancellation didn't work, exiting the TestClient context would hang for
    up to an hour instead of returning immediately.
    """
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver") as client:
        resp = client.get("/health")
        assert resp.status_code == 200
    # Reaching this line at all (rather than hanging in the with-block's
    # __exit__) is the assertion.


# --- Discover 4K queue (v0.5.0) -----------------------------------------------

#: Jellyseerr's user list, trimmed: plexId 222222 is the friend "Sam" below,
#: mapping to Jellyseerr user 4. Attribution runs through the same lookup the
#: member-facing request route uses, so the fixture shape has to match.
_JELLYSEERR_USERS = {
    "results": [
        {"id": 1, "plexId": 111111, "displayName": "Ada"},
        {"id": 4, "plexId": 222222, "displayName": "Sam"},
    ]
}

_FRIEND_PLEX_ID = 222222

_4K_ROUTES = {
    ("GET", "/api/v1/user"): httpx.Response(200, json=_JELLYSEERR_USERS),
    ("POST", "/api/v1/request"): httpx.Response(201, json={"id": 77, "status": 1}),
}


def _insert_4k_request(
    settings, *, media_type: str = "movie", tmdb_id: int = 550, title: str = "Fight Club",
    seasons: list[int] | None = None, state: str = "pending",
) -> int:
    """Insert a discover_4k_requests row directly, bypassing the Discover route."""
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        cur = conn.execute(
            "INSERT INTO discover_4k_requests (media_type, tmdb_id, title, seasons_json,"
            " requested_by, requested_by_name, state, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'Sam', ?, ?)",
            (
                media_type, tmdb_id, title,
                json.dumps(seasons) if seasons else None,
                _FRIEND_PLEX_ID, state, datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _read_4k(settings, req_id: int):
    conn = connect(settings.db_path)
    try:
        return conn.execute(
            "SELECT * FROM discover_4k_requests WHERE id = ?", (req_id,)
        ).fetchone()
    finally:
        conn.close()


def _request_bodies(router: _Router) -> list:
    return [
        json.loads(r.content)
        for r in router.requests
        if r.method == "POST" and r.url.path == "/api/v1/request"
    ]


def test_queue_includes_pending_4k_requests(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)
        _insert_4k_request(settings, tmdb_id=999, title="Settled", state="approved")

        rows = client.get("/api/admin/queue").json()["discover_4k"]
        assert [r["id"] for r in rows] == [req_id]
        assert rows[0]["title"] == "Fight Club"
        assert rows[0]["requested_by_name"] == "Sam"
    finally:
        client.__exit__(None, None, None)


def test_approve_4k_movie_files_as_the_friend_in_the_4k_lane(tmp_path):
    client, settings, router = _make_client(tmp_path, _4K_ROUTES)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)

        resp = client.post(f"/api/admin/discover-4k/{req_id}/approve")
        assert resp.status_code == 200
        assert resp.json() == {"state": "approved", "request_id": 77}

        # userId 4 is Sam's Jellyseerr id -- never the owner's, even though
        # the owner is the one pressing the button.
        assert _request_bodies(router) == [
            {
                "mediaType": "movie", "mediaId": 550, "userId": 4,
                "profileId": settings.radarr_profile_4k_id,
            }
        ]
        assert _read_4k(settings, req_id)["state"] == "approved"
    finally:
        client.__exit__(None, None, None)


def test_approve_4k_tv_files_the_stored_seasons_in_the_sonarr_4k_lane(tmp_path):
    client, settings, router = _make_client(tmp_path, _4K_ROUTES)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(
            settings, media_type="tv", tmdb_id=1399, title="Thrones", seasons=[2, 3]
        )

        assert client.post(f"/api/admin/discover-4k/{req_id}/approve").status_code == 200
        assert _request_bodies(router) == [
            {
                "mediaType": "tv", "mediaId": 1399, "userId": 4, "seasons": [2, 3],
                "profileId": settings.sonarr_profile_4k_id,
            }
        ]
    finally:
        client.__exit__(None, None, None)


def test_approve_4k_writes_the_title_hint(tmp_path):
    """The Pipeline board has nothing to enrich from until the arrs catch up."""
    client, settings, _router = _make_client(tmp_path, _4K_ROUTES)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)
        client.post(f"/api/admin/discover-4k/{req_id}/approve")

        conn = connect(settings.db_path)
        try:
            hint = conn.execute("SELECT * FROM title_hints").fetchone()
        finally:
            conn.close()
        assert (hint["media_type"], hint["tmdb_id"], hint["title"]) == ("movie", 550, "Fight Club")
    finally:
        client.__exit__(None, None, None)


def test_approve_4k_502_leaves_the_row_pending_for_a_retry(tmp_path):
    routes = {**_4K_ROUTES, ("POST", "/api/v1/request"): httpx.Response(500)}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)

        resp = client.post(f"/api/admin/discover-4k/{req_id}/approve")
        assert resp.status_code == 502
        assert resp.json() == {"error": "jellyseerr unreachable"}
        assert _read_4k(settings, req_id)["state"] == "pending"
    finally:
        client.__exit__(None, None, None)


def test_approve_4k_duplicate_settles_the_row_with_a_note(tmp_path):
    """Already upstream means the work is done; retrying forever helps nobody."""
    routes = {
        **_4K_ROUTES,
        ("POST", "/api/v1/request"): httpx.Response(
            409, json={"message": "Request for this media already exists"}
        ),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)

        resp = client.post(f"/api/admin/discover-4k/{req_id}/approve")
        assert resp.status_code == 200
        assert resp.json()["state"] == "approved"

        row = _read_4k(settings, req_id)
        assert row["state"] == "approved"
        assert "already exists" in row["note"]
    finally:
        client.__exit__(None, None, None)


def test_approve_4k_upstream_401_does_not_log_the_owner_out(tmp_path):
    """A 401 relayed verbatim would trip the client's session handler."""
    routes = {
        **_4K_ROUTES,
        ("POST", "/api/v1/request"): httpx.Response(401, json={"message": "Unauthorized"}),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)

        resp = client.post(f"/api/admin/discover-4k/{req_id}/approve")
        assert resp.status_code == 502
        assert _read_4k(settings, req_id)["state"] == "pending"
    finally:
        client.__exit__(None, None, None)


def test_approve_4k_502_when_the_friend_cannot_be_mapped(tmp_path):
    routes = {
        **_4K_ROUTES,
        ("GET", "/api/v1/user"): httpx.Response(200, json={"results": []}),
        ("POST", "/api/v1/user/import-from-plex"): httpx.Response(201, json=[]),
    }
    client, settings, router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)

        resp = client.post(f"/api/admin/discover-4k/{req_id}/approve")
        assert resp.status_code == 502
        assert "Sam" in resp.json()["error"]
        assert _request_bodies(router) == []
        assert _read_4k(settings, req_id)["state"] == "pending"
    finally:
        client.__exit__(None, None, None)


def test_approve_4k_404_and_409(tmp_path):
    client, settings, _router = _make_client(tmp_path, _4K_ROUTES)
    try:
        _login(client, settings)
        assert client.post("/api/admin/discover-4k/999/approve").status_code == 404
        settled = _insert_4k_request(settings, state="denied")
        assert client.post(f"/api/admin/discover-4k/{settled}/approve").status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_deny_4k_still_files_the_title_at_1080p(tmp_path):
    """Turning down 4K is a decision about size, not about access."""
    client, settings, router = _make_client(tmp_path, _4K_ROUTES)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)

        resp = client.post(
            f"/api/admin/discover-4k/{req_id}/deny", json={"note": "too big"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"state": "denied", "request_id": 77}

        assert _request_bodies(router) == [
            {
                "mediaType": "movie", "mediaId": 550, "userId": 4,
                "profileId": settings.radarr_profile_hd_id,
            }
        ]
        row = _read_4k(settings, req_id)
        assert row["state"] == "denied"
        assert row["note"] == "too big"
    finally:
        client.__exit__(None, None, None)


def test_deny_4k_tv_files_the_stored_seasons_at_1080p(tmp_path):
    client, settings, router = _make_client(tmp_path, _4K_ROUTES)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(
            settings, media_type="tv", tmdb_id=1399, title="Thrones", seasons=[1]
        )

        assert client.post(f"/api/admin/discover-4k/{req_id}/deny").status_code == 200
        assert _request_bodies(router) == [
            {
                "mediaType": "tv", "mediaId": 1399, "userId": 4, "seasons": [1],
                "profileId": settings.sonarr_profile_hd_id,
            }
        ]
    finally:
        client.__exit__(None, None, None)


def test_deny_4k_records_the_upstream_refusal_but_still_settles(tmp_path):
    routes = {
        **_4K_ROUTES,
        ("POST", "/api/v1/request"): httpx.Response(
            409, json={"message": "Request for this media already exists"}
        ),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)

        resp = client.post(f"/api/admin/discover-4k/{req_id}/deny", json={"note": "too big"})
        assert resp.status_code == 200
        row = _read_4k(settings, req_id)
        assert row["state"] == "denied"
        assert "too big" in row["note"] and "already exists" in row["note"]
    finally:
        client.__exit__(None, None, None)


def test_deny_4k_502_leaves_the_row_pending(tmp_path):
    """A denial that never filed the consolation copy is not finished."""
    routes = {**_4K_ROUTES, ("POST", "/api/v1/request"): httpx.Response(503)}
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)

        resp = client.post(f"/api/admin/discover-4k/{req_id}/deny")
        assert resp.status_code == 502
        assert _read_4k(settings, req_id)["state"] == "pending"
    finally:
        client.__exit__(None, None, None)


def _subscribe_friend(settings) -> None:
    """Give Sam a push endpoint so outcome notifications have somewhere to go."""
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        conn.execute(
            "INSERT INTO push_subscriptions (plex_account_id, endpoint, keys_json, created_at)"
            " VALUES (?, 'https://p/sam', ?, ?)",
            (
                _FRIEND_PLEX_ID,
                json.dumps({"p256dh": "p256dh-key", "auth": "auth-key"}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("action", "expected_title", "expected_body"),
    [
        ("approve", "Request approved", "Fight Club"),
        ("deny", "4K declined", "grabbing Fight Club in 1080p instead"),
    ],
)
def test_4k_outcome_push_goes_to_the_requester(
    tmp_path, monkeypatch, action, expected_title, expected_body
):
    """The friend hears the outcome -- and a denial says what they DO get."""
    from pensieve import push

    calls = []

    def recorder(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(push, "webpush", recorder)

    client, settings, _router = _make_client(tmp_path, _4K_ROUTES)
    try:
        _login(client, settings)
        _subscribe_friend(settings)
        req_id = _insert_4k_request(settings)

        assert client.post(f"/api/admin/discover-4k/{req_id}/{action}").status_code == 200

        assert len(calls) == 1
        payload = json.loads(calls[0]["data"])
        assert calls[0]["subscription_info"]["endpoint"] == "https://p/sam"
        assert payload["title"] == expected_title
        assert payload["body"] == expected_body
        assert payload["tab"] == "pipeline"
    finally:
        client.__exit__(None, None, None)


def test_deny_4k_leaves_the_row_pending_when_the_1080p_copy_was_refused(tmp_path):
    """A non-duplicate refusal filed nothing, so the denial is not finished.

    Settling here would drop the row out of the queue *and* push "grabbing it
    in 1080p instead" at a friend who is getting nothing — the exact
    stranding this queue exists to prevent. Only a 409 (the copy is already
    on its way) is allowed to settle it.
    """
    for offset, status in enumerate((400, 401, 403, 404, 429)):
        routes = {
            **_4K_ROUTES,
            ("POST", "/api/v1/request"): httpx.Response(
                status, json={"message": "You do not have permission"}
            ),
        }
        client, settings, _router = _make_client(tmp_path, routes)
        try:
            _login(client, settings)
            # A distinct title per pass: the rows all stay pending (that is
            # the point), and the pending-uniqueness index would refuse a
            # second row for the same tmdb_id in this shared tmp_path DB.
            req_id = _insert_4k_request(settings, tmdb_id=550 + offset)

            resp = client.post(
                f"/api/admin/discover-4k/{req_id}/deny", json={"note": "too big"}
            )
            # Never the upstream's own status: a relayed 401 would log the
            # owner out of the app mid-approval.
            assert resp.status_code == 502, status
            assert _read_4k(settings, req_id)["state"] == "pending", status
        finally:
            client.__exit__(None, None, None)


def test_deny_4k_refusal_does_not_tell_the_friend_a_copy_is_coming(tmp_path, monkeypatch):
    """No filing, no outcome push — the friend is not told anything landed."""
    from pensieve import push

    calls: list = []
    monkeypatch.setattr(push, "webpush", lambda **kwargs: calls.append(kwargs))

    routes = {
        **_4K_ROUTES,
        ("POST", "/api/v1/request"): httpx.Response(403, json={"message": "nope"}),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    try:
        _login(client, settings)
        _subscribe_friend(settings)
        req_id = _insert_4k_request(settings)

        assert client.post(f"/api/admin/discover-4k/{req_id}/deny").status_code == 502
        assert calls == []
    finally:
        client.__exit__(None, None, None)


def test_a_settled_4k_row_cannot_be_settled_twice(tmp_path):
    """The compare-and-swap: two owner sessions, one outcome.

    ``_load_pending_4k`` reads the state several awaits before the write, so
    a second session can pass the same gate. Simulated by settling the row
    underneath an in-flight approve.
    """
    client, settings, _router = _make_client(tmp_path, _4K_ROUTES)
    try:
        _login(client, settings)
        req_id = _insert_4k_request(settings)

        import pensieve.api.admin_routes as ar

        original = ar._file_discover_4k_row

        async def _settle_mid_flight(*args, **kwargs):
            """Stand in for the other session finishing first."""
            conn = connect(settings.db_path)
            try:
                conn.execute(
                    "UPDATE discover_4k_requests SET state = 'denied' WHERE id = ?", (req_id,)
                )
                conn.commit()
            finally:
                conn.close()
            return await original(*args, **kwargs)

        ar._file_discover_4k_row = _settle_mid_flight
        try:
            resp = client.post(f"/api/admin/discover-4k/{req_id}/approve")
        finally:
            ar._file_discover_4k_row = original

        assert resp.status_code == 409
        # The other session's outcome stands; it is not overwritten.
        assert _read_4k(settings, req_id)["state"] == "denied"
    finally:
        client.__exit__(None, None, None)
