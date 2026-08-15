"""Tests for the v0.2.0 request-access flow.

Covers the three surfaces it spans: the guest cookie the denied login branch
hands out, the unauthenticated ``/api/guest`` routes a would-be member uses to
ask for access, and the owner-side approve/deny pair that actually calls
plex.tv's share API.
"""
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from pensieve.auth import GUEST_COOKIE, SESSION_COOKIE, sign_guest, sign_session
from pensieve.clients.base import CachedHTTP
from pensieve.db import connect, init_db
from pensieve.main import create_app
from pensieve.ratelimit import auth_limiter
from tests.conftest import make_settings, seed_user

# plex.tv answers GET /api/servers/{machine_id} with XML no matter what the
# Accept header says -- which is the whole reason CachedHTTP grew get_text.
_SERVERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer friendlyName="HomeServer">
  <Server name="HomeServer" machineIdentifier="machine-123">
    <Section id="3" key="1" title="Movies" type="movie"/>
    <Section id="5" key="2" title="TV Shows" type="show"/>
  </Server>
</MediaContainer>
"""


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The guest routes share the module-level login limiter."""
    auth_limiter._hits.clear()
    yield
    auth_limiter._hits.clear()


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


def _login(client: TestClient, settings, *, user_id: int = 1, name: str = "Ada",
           role: str = "owner") -> None:
    seed_user(settings, user_id=user_id, name=name, role=role)
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": user_id, "name": name, "role": role})
    )


def _guest(client: TestClient, settings, *, user_id: int = 77, name: str = "Neville",
           email: str | None = "neville@example.com") -> None:
    client.cookies.set(
        GUEST_COOKIE, sign_guest(settings, {"id": user_id, "name": name, "email": email or ""})
    )


def _insert_access_request(
    settings, *, plex_account_id: int = 77, name: str = "Neville",
    email: str = "neville@example.com", state: str = "pending",
) -> int:
    """Insert an access_requests row directly, bypassing the guest route."""
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        cur = conn.execute(
            "INSERT INTO access_requests (plex_account_id, name, email, state, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (plex_account_id, name, email, state, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _read_access_request(settings, req_id: int) -> dict:
    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM access_requests WHERE id = ?", (req_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# --- the denied login branch hands out a guest cookie -------------------------


def test_denied_login_sets_guest_cookie_with_email(tmp_path):
    """The denial redirect must carry enough identity to file a request."""
    from pensieve.auth import read_guest

    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/pins" and request.method == "POST":
            return httpx.Response(201, json={"id": 111, "code": "abcd"})
        if path == "/api/v2/pins/111":
            return httpx.Response(200, json={"id": 111, "authToken": "tok-1"})
        if path == "/api/v2/user":
            return httpx.Response(200, json={
                "id": 77, "username": "nev", "friendlyName": "Neville",
                "thumb": "t", "email": "neville@example.com",
            })
        if path == "/api/v2/resources":
            return httpx.Response(200, json=[{"clientIdentifier": "someone-else"}])
        return httpx.Response(404)

    client, settings, _router = _make_client(tmp_path, {})
    client.app.state.http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(route)))
    try:
        client.get("/api/auth/login")
        resp = client.get("/auth/callback", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?denied=1"

        raw = client.cookies.get(GUEST_COOKIE)
        assert raw is not None
        assert read_guest(settings, raw) == {
            "id": 77, "name": "Neville", "email": "neville@example.com",
        }
    finally:
        client.__exit__(None, None, None)


# --- POST /api/guest/access-requests -----------------------------------------


def test_post_access_request_without_guest_cookie_is_401(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        resp = client.post("/api/guest/access-requests")
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_post_access_request_creates_row_and_fires_webhook(tmp_path):
    client, settings, router = _make_client(
        tmp_path, {}, discord_webhook_url="http://discord.test/hook"
    )
    try:
        _guest(client, settings)
        resp = client.post("/api/guest/access-requests")
        assert resp.status_code == 201
        assert resp.json() == {"state": "pending"}

        conn = connect(settings.db_path)
        row = conn.execute("SELECT * FROM access_requests").fetchone()
        events = conn.execute("SELECT * FROM events").fetchall()
        conn.close()
        assert row["plex_account_id"] == 77
        assert row["name"] == "Neville"
        assert row["email"] == "neville@example.com"
        assert row["state"] == "pending"
        assert len(events) == 1

        hook = [r for r in router.requests if r.url.path == "/hook"]
        assert len(hook) == 1
        sent = json.loads(hook[0].content)
        assert "Neville" in sent["content"]
        assert "neville@example.com" in sent["content"]
        assert sent["allowed_mentions"] == {"parse": []}
    finally:
        client.__exit__(None, None, None)


def test_second_post_is_idempotent_and_does_not_re_notify(tmp_path):
    client, settings, router = _make_client(
        tmp_path, {}, discord_webhook_url="http://discord.test/hook"
    )
    try:
        _guest(client, settings)
        assert client.post("/api/guest/access-requests").status_code == 201

        again = client.post("/api/guest/access-requests")
        assert again.status_code == 200
        assert again.json() == {"state": "pending"}

        conn = connect(settings.db_path)
        count = conn.execute("SELECT COUNT(*) AS n FROM access_requests").fetchone()["n"]
        conn.close()
        assert count == 1
        assert len([r for r in router.requests if r.url.path == "/hook"]) == 1
    finally:
        client.__exit__(None, None, None)


def test_post_after_denial_is_409(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _insert_access_request(settings, state="denied")
        _guest(client, settings)
        resp = client.post("/api/guest/access-requests")
        assert resp.status_code == 409
        assert resp.json() == {"error": "access was declined"}
    finally:
        client.__exit__(None, None, None)


def test_post_after_approval_reports_approved(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _insert_access_request(settings, state="approved")
        _guest(client, settings)
        resp = client.post("/api/guest/access-requests")
        assert resp.status_code == 200
        assert resp.json() == {"state": "approved"}
    finally:
        client.__exit__(None, None, None)


def test_post_without_email_on_the_plex_account_is_400(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _guest(client, settings, email=None)
        resp = client.post("/api/guest/access-requests")
        assert resp.status_code == 400
        assert resp.json() == {"error": "no email on plex account"}
    finally:
        client.__exit__(None, None, None)


def test_tampered_guest_cookie_is_401(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _guest(client, settings)
        good = client.cookies.get(GUEST_COOKIE)
        client.cookies.set(GUEST_COOKIE, good[:-1] + ("a" if good[-1] != "a" else "b"))
        assert client.post("/api/guest/access-requests").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_session_cookie_presented_as_guest_cookie_is_401(tmp_path):
    """Distinct salts: a real session token must not pass as a guest token."""
    client, settings, _router = _make_client(tmp_path, {})
    try:
        client.cookies.set(
            GUEST_COOKIE, sign_session(settings, {"id": 1, "name": "Ada", "role": "owner"})
        )
        assert client.post("/api/guest/access-requests").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_guest_routes_are_rate_limited(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _guest(client, settings)
        for _ in range(10):
            assert client.get("/api/guest/access-requests/me").status_code == 200
        assert client.get("/api/guest/access-requests/me").status_code == 429
    finally:
        client.__exit__(None, None, None)


# --- GET /api/guest/access-requests/me ---------------------------------------


def test_me_reports_none_before_any_request(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _guest(client, settings)
        resp = client.get("/api/guest/access-requests/me")
        assert resp.status_code == 200
        assert resp.json() == {"state": "none"}
    finally:
        client.__exit__(None, None, None)


def test_me_reports_the_row_state(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _insert_access_request(settings, state="pending")
        _guest(client, settings)
        assert client.get("/api/guest/access-requests/me").json() == {"state": "pending"}
    finally:
        client.__exit__(None, None, None)


def test_me_without_guest_cookie_is_401(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        assert client.get("/api/guest/access-requests/me").status_code == 401
    finally:
        client.__exit__(None, None, None)


# --- owner queue + approve/deny ----------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/admin/access-requests/1/approve"),
        ("POST", "/api/admin/access-requests/1/deny"),
    ],
)
def test_member_403_on_admin_access_routes(tmp_path, method, path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings, role="member")
        assert client.request(method, path).status_code == 403
    finally:
        client.__exit__(None, None, None)


def test_queue_lists_pending_access_requests_only(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        _insert_access_request(settings, plex_account_id=77, state="pending")
        _insert_access_request(settings, plex_account_id=88, name="Luna",
                               email="luna@example.com", state="denied")

        body = client.get("/api/admin/queue").json()
        assert [r["plex_account_id"] for r in body["access"]] == [77]
        assert body["access"][0]["email"] == "neville@example.com"
    finally:
        client.__exit__(None, None, None)


def test_approve_shares_the_library_and_creates_the_member(tmp_path):
    routes = {
        ("GET", "/api/servers/machine-123"): httpx.Response(200, text=_SERVERS_XML),
        ("POST", "/api/servers/machine-123/shared_servers"): httpx.Response(200, json={}),
    }
    client, settings, router = _make_client(tmp_path, routes, plex_owner_token="ot")
    try:
        _login(client, settings)
        req_id = _insert_access_request(settings)

        resp = client.post(f"/api/admin/access-requests/{req_id}/approve")
        assert resp.status_code == 200
        assert resp.json() == {"state": "approved"}

        share = [r for r in router.requests if r.url.path.endswith("/shared_servers")]
        assert len(share) == 1
        sent = json.loads(share[0].content)
        assert sent["server_id"] == "machine-123"
        assert sent["shared_server"]["library_section_ids"] == [3, 5]
        assert sent["shared_server"]["invited_email"] == "neville@example.com"
        assert share[0].headers["X-Plex-Token"] == "ot"

        row = _read_access_request(settings, req_id)
        assert row["state"] == "approved"
        assert row["resolved_at"] is not None

        conn = connect(settings.db_path)
        user = conn.execute(
            "SELECT * FROM users WHERE plex_account_id = 77"
        ).fetchone()
        conn.close()
        assert user["name"] == "Neville"
        assert user["role"] == "member"
        assert user["revoked"] == 0
    finally:
        client.__exit__(None, None, None)


def test_approve_unrevokes_a_previously_revoked_account(tmp_path):
    routes = {
        ("GET", "/api/servers/machine-123"): httpx.Response(200, text=_SERVERS_XML),
        ("POST", "/api/servers/machine-123/shared_servers"): httpx.Response(200, json={}),
    }
    client, settings, _router = _make_client(tmp_path, routes, plex_owner_token="ot")
    try:
        _login(client, settings)
        seed_user(settings, user_id=77, name="Neville", role="member", revoked=1)
        req_id = _insert_access_request(settings)

        assert client.post(f"/api/admin/access-requests/{req_id}/approve").status_code == 200

        conn = connect(settings.db_path)
        user = conn.execute("SELECT * FROM users WHERE plex_account_id = 77").fetchone()
        conn.close()
        assert user["revoked"] == 0
    finally:
        client.__exit__(None, None, None)


def test_approve_when_plex_tv_fails_is_502_and_leaves_the_row_pending(tmp_path):
    routes = {
        ("GET", "/api/servers/machine-123"): httpx.Response(500, text="boom"),
    }
    client, settings, _router = _make_client(tmp_path, routes, plex_owner_token="ot")
    try:
        _login(client, settings)
        req_id = _insert_access_request(settings)

        resp = client.post(f"/api/admin/access-requests/{req_id}/approve")
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"] == "plex.tv unreachable"
        # Sanitized: no upstream URL/exception text leaks to the client.
        assert "http" not in body["error"]

        row = _read_access_request(settings, req_id)
        assert row["state"] == "pending"
        assert row["resolved_at"] is None

        conn = connect(settings.db_path)
        user = conn.execute("SELECT * FROM users WHERE plex_account_id = 77").fetchone()
        conn.close()
        assert user is None
    finally:
        client.__exit__(None, None, None)


def test_approve_missing_row_is_404_and_wrong_state_is_409(tmp_path):
    client, settings, _router = _make_client(tmp_path, {}, plex_owner_token="ot")
    try:
        _login(client, settings)
        assert client.post("/api/admin/access-requests/999/approve").status_code == 404

        req_id = _insert_access_request(settings, state="denied")
        assert client.post(f"/api/admin/access-requests/{req_id}/approve").status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_deny_stores_the_note_and_closes_the_row(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        req_id = _insert_access_request(settings)

        resp = client.post(
            f"/api/admin/access-requests/{req_id}/deny", json={"note": "not right now"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"state": "denied"}

        row = _read_access_request(settings, req_id)
        assert row["state"] == "denied"
        assert row["note"] == "not right now"
        assert row["resolved_at"] is not None
    finally:
        client.__exit__(None, None, None)


def test_deny_missing_row_is_404_and_resolved_row_is_409(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        assert client.post("/api/admin/access-requests/999/deny").status_code == 404

        req_id = _insert_access_request(settings, state="approved")
        assert client.post(f"/api/admin/access-requests/{req_id}/deny").status_code == 409
    finally:
        client.__exit__(None, None, None)
