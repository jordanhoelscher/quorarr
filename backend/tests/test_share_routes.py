"""Routes that surface a member's Plex share state.

The gap these close: since 0.5.2 an owner approval alone gets someone into
the app, so an approved friend can browse everything while Plex has never
shared the server with them. Jellyseerr therefore has no account for them and
every request fails — previously with advice to ask the owner who had already
approved them.
"""

import httpx
from fastapi.testclient import TestClient

from pensieve.auth import SESSION_COOKIE, sign_session
from pensieve.clients.base import CachedHTTP
from pensieve.db import connect, init_db
from pensieve.main import create_app
from tests.conftest import make_settings, seed_user

_INVITES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="1">
  <Invite id="700000003" createdAt="1700000000" username="morgan" email="w@example.com"/>
</MediaContainer>"""

_SHARED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="1">
<SharedServer id="10000002" username="robin" email="l@example.com" userID="700000002"/>
</MediaContainer>"""

_PLEX_ROUTES = {
    ("GET", "/api/invites/requested"): httpx.Response(200, text=_INVITES_XML),
    ("GET", "/api/servers/machine-123/shared_servers"): httpx.Response(200, text=_SHARED_XML),
}


class _Router:
    """Returns canned responses by (method, path); 404 for anything else."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self.routes = routes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = self.routes.get((request.method, request.url.path))
        if response is None:
            return httpx.Response(404)
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )


def _make_client(tmp_path, routes: dict, **overrides):
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    app.state.http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(_Router(routes))))
    return client, settings


def _sign_in(client: TestClient, settings, *, user_id: int, name: str = "Sam",
             role: str = "member") -> None:
    """Seed the users row and set the cookie. Role is explicit here because
    these tests need both a member and the owner, at arbitrary account ids."""
    seed_user(settings, user_id=user_id, name=name, role=role)
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": user_id, "name": name, "role": role})
    )


# ------------------------------------------------------------- /api/me/share


def test_me_share_reports_active_for_a_shared_account(tmp_path):
    client, settings = _make_client(tmp_path, _PLEX_ROUTES)
    try:
        _sign_in(client, settings, user_id=700000002)
        assert client.get("/api/me/share").json() == {"state": "active"}
    finally:
        client.__exit__(None, None, None)


def test_me_share_reports_pending_for_an_unaccepted_invite(tmp_path):
    client, settings = _make_client(tmp_path, _PLEX_ROUTES)
    try:
        _sign_in(client, settings, user_id=700000003)
        assert client.get("/api/me/share").json() == {"state": "pending"}
    finally:
        client.__exit__(None, None, None)


def test_me_share_reports_none_when_plex_knows_nothing_of_them(tmp_path):
    client, settings = _make_client(tmp_path, _PLEX_ROUTES)
    try:
        _sign_in(client, settings, user_id=999999)
        assert client.get("/api/me/share").json() == {"state": "none"}
    finally:
        client.__exit__(None, None, None)


def test_me_share_reports_unknown_rather_than_failing_when_plex_is_down(tmp_path):
    """200 with "unknown", never a 5xx: this endpoint decorates a view."""
    client, settings = _make_client(tmp_path, {})
    try:
        _sign_in(client, settings, user_id=700000002)
        resp = client.get("/api/me/share")
        assert resp.status_code == 200
        assert resp.json() == {"state": "unknown"}
    finally:
        client.__exit__(None, None, None)


def test_me_share_requires_a_session(tmp_path):
    client, settings = _make_client(tmp_path, _PLEX_ROUTES)
    try:
        assert client.get("/api/me/share").status_code == 401
    finally:
        client.__exit__(None, None, None)


# ------------------------------------------------------- queue waiting_on_plex


def test_queue_lists_members_waiting_on_plex(tmp_path):
    client, settings = _make_client(tmp_path, _PLEX_ROUTES, plex_owner_account_id=1)
    try:
        _sign_in(client, settings, user_id=1, name="Ada", role="owner")
        seed_user(settings, user_id=700000003, name="morgan")
        seed_user(settings, user_id=700000002, name="robin")

        waiting = client.get("/api/admin/queue").json()["waiting_on_plex"]
        assert [w["plex_account_id"] for w in waiting] == [700000003]
        assert waiting[0]["name"] == "morgan"
        assert waiting[0]["email"] == "w@example.com"
        assert waiting[0]["invited_at"] == 1700000000
    finally:
        client.__exit__(None, None, None)


def test_queue_excludes_revoked_members_from_waiting_on_plex(tmp_path):
    client, settings = _make_client(tmp_path, _PLEX_ROUTES, plex_owner_account_id=1)
    try:
        _sign_in(client, settings, user_id=1, name="Ada", role="owner")
        seed_user(settings, user_id=700000003, name="morgan", revoked=1)

        assert client.get("/api/admin/queue").json()["waiting_on_plex"] == []
    finally:
        client.__exit__(None, None, None)


def test_queue_excludes_the_owner_from_waiting_on_plex(tmp_path):
    """The owner owns the server and is never in either plex.tv list."""
    client, settings = _make_client(tmp_path, _PLEX_ROUTES, plex_owner_account_id=700000003)
    try:
        _sign_in(client, settings, user_id=700000003, name="Ada", role="owner")
        assert client.get("/api/admin/queue").json()["waiting_on_plex"] == []
    finally:
        client.__exit__(None, None, None)


def test_queue_survives_plex_being_down(tmp_path):
    """The four pre-existing queues must not go down with a nice-to-have."""
    client, settings = _make_client(tmp_path, {}, plex_owner_account_id=1)
    try:
        _sign_in(client, settings, user_id=1, name="Ada", role="owner")
        resp = client.get("/api/admin/queue")
        assert resp.status_code == 200
        body = resp.json()
        assert body["waiting_on_plex"] == []
        for key in ("deletions", "quality", "access", "discover_4k"):
            assert key in body
    finally:
        client.__exit__(None, None, None)
