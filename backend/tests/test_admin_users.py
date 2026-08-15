"""Owner-only user administration: list, revoke, unrevoke."""

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


def _login_owner(client: TestClient, settings) -> None:
    seed_user(settings, user_id=1, name="Ada", role="owner")
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": 1, "name": "Ada", "role": "owner"})
    )


def test_list_users_returns_role_and_revoked(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        _login_owner(client, settings)
        seed_user(settings, user_id=2, name="Sam", role="member")

        resp = client.get("/api/admin/users")
        assert resp.status_code == 200
        users = resp.json()["users"]
        by_id = {u["id"]: u for u in users}
        assert by_id[2]["name"] == "Sam"
        assert by_id[2]["role"] == "member"
        assert by_id[2]["revoked"] == 0
        assert "last_seen" in by_id[2]
    finally:
        client.__exit__(None, None, None)


def test_revoke_then_unrevoke_round_trip(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        _login_owner(client, settings)
        seed_user(settings, user_id=2, name="Sam", role="member")

        revoke = client.post("/api/admin/users/2/revoke")
        assert revoke.status_code == 200
        assert revoke.json() == {"id": 2, "revoked": 1}

        member = TestClient(client.app, base_url="https://testserver")
        member.cookies.set(
            SESSION_COOKIE, sign_session(settings, {"id": 2, "name": "Sam", "role": "member"})
        )
        assert member.get("/api/flags").status_code == 401

        unrevoke = client.post("/api/admin/users/2/unrevoke")
        assert unrevoke.status_code == 200
        assert unrevoke.json() == {"id": 2, "revoked": 0}
        assert member.get("/api/flags").status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_revoke_unknown_user_404(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        _login_owner(client, settings)
        assert client.post("/api/admin/users/999/revoke").status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_owner_cannot_revoke_themselves_409(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        _login_owner(client, settings)
        resp = client.post("/api/admin/users/1/revoke")
        assert resp.status_code == 409
        assert "error" in resp.json()
        # ...and is still able to use the app.
        assert client.get("/api/admin/users").status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_member_cannot_reach_user_admin_routes(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        seed_user(settings, user_id=2, name="Sam", role="member")
        client.cookies.set(
            SESSION_COOKIE, sign_session(settings, {"id": 2, "name": "Sam", "role": "member"})
        )
        assert client.get("/api/admin/users").status_code == 403
        assert client.post("/api/admin/users/2/unrevoke").status_code == 403
    finally:
        client.__exit__(None, None, None)
