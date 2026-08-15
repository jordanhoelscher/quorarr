"""Session authorization is DB-authoritative, not cookie-authoritative.

The signed cookie proves *who* you are; the ``users`` row decides whether you
still have access and what role you hold. That's what makes revocation (and a
role demotion) take effect on the next request rather than whenever a 30-day
cookie happens to expire.
"""

import httpx
from fastapi.testclient import TestClient

from pensieve.auth import SESSION_COOKIE, sign_session
from pensieve.clients.base import CachedHTTP
from pensieve.main import create_app
from tests.conftest import make_settings, seed_user


def _make_client(tmp_path, **overrides) -> tuple[TestClient, object]:
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    app.state.http = CachedHTTP(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    )
    return client, settings


def _cookie(client: TestClient, settings, *, user_id: int, name: str, role: str) -> None:
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": user_id, "name": name, "role": role})
    )


def test_revoked_member_valid_cookie_is_401(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        seed_user(settings, user_id=2, name="Sam", role="member")
        _cookie(client, settings, user_id=2, name="Sam", role="member")
        assert client.get("/api/flags").status_code == 200

        seed_user(settings, user_id=2, name="Sam", role="member", revoked=1)
        assert client.get("/api/flags").status_code == 401
        assert client.get("/api/auth/me").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_session_without_user_row_is_401(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        _cookie(client, settings, user_id=99, name="Ghost", role="member")
        assert client.get("/api/flags").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_db_role_overrides_cookie_role_for_owner_gate(tmp_path):
    """A cookie minted while the user was an owner must not outlive the demotion."""
    client, settings = _make_client(tmp_path)
    try:
        seed_user(settings, user_id=1, name="Ada", role="member")
        _cookie(client, settings, user_id=1, name="Ada", role="owner")
        assert client.get("/api/admin/queue").status_code == 403
        assert client.get("/api/auth/me").json()["role"] == "member"

        # ...and the reverse: a member-role cookie on a promoted account.
        seed_user(settings, user_id=1, name="Ada", role="owner")
        _cookie(client, settings, user_id=1, name="Ada", role="member")
        assert client.get("/api/admin/queue").status_code == 200
        assert client.get("/api/auth/me").json()["role"] == "owner"
    finally:
        client.__exit__(None, None, None)
