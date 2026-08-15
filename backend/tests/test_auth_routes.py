import httpx
import pytest
from fastapi.testclient import TestClient

from pensieve.clients.base import CachedHTTP
from pensieve.db import connect
from pensieve.main import create_app
from pensieve.ratelimit import auth_limiter
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The auth rate limiter is module-level state shared across tests."""
    auth_limiter._hits.clear()
    yield
    auth_limiter._hits.clear()


def _route(request: httpx.Request) -> httpx.Response:
    """Mock plex.tv: PIN gets claimed by account 42, which has our server."""
    path = request.url.path
    if path == "/api/v2/pins" and request.method == "POST":
        return httpx.Response(201, json={"id": 111, "code": "abcd"})
    if path == "/api/v2/pins/111":
        # plex.tv echoes the code on every poll (verified live 2026-08-13);
        # the callback checks it against the cookie's copy.
        return httpx.Response(200, json={"id": 111, "code": "abcd", "authToken": "tok-1"})
    if path == "/api/v2/user":
        return httpx.Response(
            200, json={"id": 42, "username": "sam", "friendlyName": "Sam", "thumb": "t"}
        )
    if path == "/api/v2/resources":
        return httpx.Response(
            200, json=[{"name": "HomeServer", "clientIdentifier": "machine-123"}]
        )
    return httpx.Response(404)


def _denied_route(request: httpx.Request) -> httpx.Response:
    """Same as _route, but the account has no share on our server."""
    if request.url.path == "/api/v2/resources":
        return httpx.Response(
            200, json=[{"name": "SomeoneElses", "clientIdentifier": "other"}]
        )
    return _route(request)


def _make_client(tmp_path, route=_route, **overrides) -> tuple[TestClient, object]:
    """Build a TestClient with the app's http swapped for a mocked transport.

    Returns (client, settings). Caller is responsible for entering/exiting
    the TestClient as a context manager (lifespan sets app.state.http, which
    we then overwrite with the mock so no test hits the real network).
    """
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings)
    # https base_url so the `secure=True` cookies we set in production are
    # actually persisted/sent back by the test client's cookie jar.
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    app.state.http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(route)))
    return client, settings


def test_login_returns_auth_url_and_sets_pin_cookie(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        resp = client.get("/api/auth/login")
        assert resp.status_code == 200
        body = resp.json()
        assert "clientID=pensieve-test" in body["auth_url"]
        assert "code=abcd" in body["auth_url"]
        assert client.cookies.get("pensieve_pin") is not None
    finally:
        client.__exit__(None, None, None)


def test_eleventh_login_from_same_ip_is_rate_limited(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        for _ in range(10):
            resp = client.get("/api/auth/login")
            assert resp.status_code == 200
        resp = client.get("/api/auth/login")
        assert resp.status_code == 429
    finally:
        client.__exit__(None, None, None)


def test_callback_sets_session_cookie_and_upserts_member(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        login = client.get("/api/auth/login")
        assert login.status_code == 200

        resp = client.get("/auth/callback", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert client.cookies.get("pensieve_session") is not None

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json() == {"id": 42, "name": "Sam", "role": "member"}

        conn = connect(settings.db_path)
        row = conn.execute(
            "SELECT plex_account_id, name, role FROM users WHERE plex_account_id=42"
        ).fetchone()
        conn.close()
        assert row["name"] == "Sam"
        assert row["role"] == "member"
    finally:
        client.__exit__(None, None, None)


def test_callback_grants_owner_role_when_id_matches_settings(tmp_path):
    client, settings = _make_client(tmp_path, plex_owner_account_id=42)
    try:
        client.get("/api/auth/login")
        client.get("/auth/callback", follow_redirects=False)

        me = client.get("/api/auth/me")
        assert me.json()["role"] == "owner"
    finally:
        client.__exit__(None, None, None)


def test_callback_denies_when_account_lacks_server_access(tmp_path):
    client, settings = _make_client(tmp_path, route=_denied_route)
    try:
        client.get("/api/auth/login")
        resp = client.get("/auth/callback", follow_redirects=False)
        # Redirect back into the SPA, which reads ?denied=1 and explains what
        # to do -- not a dead-end 403 page outside the app.
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?denied=1"
        assert client.cookies.get("pensieve_session") is None
        # The pin cookie should be cleared on the denied path too, same as
        # the success path -- it's single-use and shouldn't linger.
        assert client.cookies.get("pensieve_pin") is None
        # ... but a short-lived guest cookie takes its place, so the login
        # screen can offer "Request access" instead of a dead end.
        assert client.cookies.get("pensieve_guest") is not None
    finally:
        client.__exit__(None, None, None)


def _seed_user(settings, plex_account_id: int, *, name: str, revoked: int = 0) -> None:
    """Put a users row in place, as `POST /access-requests/{id}/approve` does."""
    conn = connect(settings.db_path)
    conn.execute(
        "INSERT INTO users (plex_account_id, name, role, last_seen, revoked)"
        " VALUES (?, ?, 'member', '2026-01-01T00:00:00+00:00', ?)",
        (plex_account_id, name, revoked),
    )
    conn.commit()
    conn.close()


def test_callback_admits_an_approved_member_who_has_not_accepted_the_plex_share(tmp_path):
    """Owner approval is sufficient on its own; the Plex invite may be unaccepted.

    Regression: approving an access request wrote the users row and sent the
    plex.tv invite, but the gate only asked plex.tv -- which reports the share
    only once the friend *accepts* the invite email. the app therefore called
    them a member and refused them at the door.
    """
    client, settings = _make_client(tmp_path, route=_denied_route)
    try:
        _seed_user(settings, 42, name="Sam")

        client.get("/api/auth/login")
        resp = client.get("/auth/callback", follow_redirects=False)

        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/"
        assert client.cookies.get("pensieve_session") is not None
        assert client.cookies.get("pensieve_guest") is None

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json() == {"id": 42, "name": "Sam", "role": "member"}
    finally:
        client.__exit__(None, None, None)


def test_callback_still_denies_a_revoked_user_without_a_plex_share(tmp_path):
    """Revoking in the app is the cut-off, so it must survive this new path."""
    client, settings = _make_client(tmp_path, route=_denied_route)
    try:
        _seed_user(settings, 42, name="Sam", revoked=1)

        client.get("/api/auth/login")
        resp = client.get("/auth/callback", follow_redirects=False)

        assert resp.status_code == 303
        assert resp.headers["location"] == "/?denied=1"
        assert client.cookies.get("pensieve_session") is None
        assert client.cookies.get("pensieve_guest") is not None
    finally:
        client.__exit__(None, None, None)


def test_callback_admits_an_approved_member_when_plex_resources_is_down(tmp_path):
    """A known member should not be locked out by a plex.tv outage.

    The users row is checked before the `/resources` round trip, so the only
    plex.tv calls a returning member depends on are the ones that produced
    their token in the first place.
    """

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/resources":
            return httpx.Response(503)
        return _route(request)

    client, settings = _make_client(tmp_path, route=route)
    try:
        _seed_user(settings, 42, name="Sam")

        client.get("/api/auth/login")
        resp = client.get("/auth/callback", follow_redirects=False)

        assert resp.status_code in (302, 307)
        assert client.cookies.get("pensieve_session") is not None
    finally:
        client.__exit__(None, None, None)


def test_callback_without_pin_cookie_is_400(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        resp = client.get("/auth/callback", follow_redirects=False)
        assert resp.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_callback_retries_poll_then_400_when_pin_never_claimed(tmp_path, monkeypatch):
    from pensieve.api import auth_routes

    async def no_sleep(_seconds):
        return None

    # Patch only auth_routes' own poll-retry delay binding, not the shared
    # asyncio module -- main.py's hourly sweep loop also awaits
    # asyncio.sleep(3600), and a global patch here would turn that into a
    # busy-spin that starves the event loop and hangs the whole test run.
    monkeypatch.setattr(auth_routes, "_sleep", no_sleep)

    def unclaimed_route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/pins/111":
            return httpx.Response(200, json={"id": 111, "authToken": None})
        return _route(request)

    client, settings = _make_client(tmp_path, route=unclaimed_route)
    try:
        client.get("/api/auth/login")
        resp = client.get("/auth/callback", follow_redirects=False)
        assert resp.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_me_requires_session_cookie(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_tampered_session_cookie_is_401(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        client.get("/api/auth/login")
        client.get("/auth/callback", follow_redirects=False)
        good_cookie = client.cookies.get("pensieve_session")
        assert good_cookie is not None

        # Tamper the *first* character (the payload), not the last. The last
        # character of a base64 signature carries spare bits that decoding
        # ignores, so several characters decode to the same signature bytes
        # -- flipping it verified fine every few dozen runs, which made this
        # test flaky for reasons that had nothing to do with what it checks.
        tampered = ("a" if good_cookie[0] != "a" else "b") + good_cookie[1:]
        client.cookies.set("pensieve_session", tampered)

        resp = client.get("/api/auth/me")
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_pin_cookie_presented_as_session_cookie_is_401_not_500(tmp_path):
    """A validly-signed PIN cookie must never verify as a session.

    Distinct salts mean this fails at signature verification; even if that
    ever regressed, current_user's shape check (must have id/name/role)
    would still reject a {"pin_id": N} payload with a clean 401 instead of
    a KeyError-turned-500 downstream.
    """
    from pensieve.auth import sign_pin

    client, settings = _make_client(tmp_path)
    try:
        pin_cookie_value = sign_pin(settings, {"pin_id": 111})
        client.cookies.set("pensieve_session", pin_cookie_value)

        resp = client.get("/api/auth/me")
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_logout_clears_session_cookie(tmp_path):
    client, settings = _make_client(tmp_path)
    try:
        client.get("/api/auth/login")
        client.get("/auth/callback", follow_redirects=False)
        assert client.get("/api/auth/me").status_code == 200

        resp = client.post("/api/auth/logout")
        assert resp.status_code == 204

        assert client.get("/api/auth/me").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_client_id_is_served_unauthenticated(tmp_path):
    """The browser needs it to mint its own PIN, before any session exists."""
    client, _settings = _make_client(tmp_path)
    try:
        resp = client.get("/api/auth/client-id")
        assert resp.status_code == 200
        assert resp.json() == {"client_id": "pensieve-test"}
    finally:
        client.__exit__(None, None, None)


def test_login_body_adopts_the_browsers_pin(tmp_path):
    """The auth URL carries the browser's code, and the cookie its PIN."""
    from pensieve.auth import read_pin

    client, settings = _make_client(tmp_path)
    try:
        resp = client.post(
            "/api/auth/login", json={"pin_id": 987654, "code": "browsercode1"}
        )
        assert resp.status_code == 200
        assert "code=browsercode1" in resp.json()["auth_url"]
        assert "clientID=pensieve-test" in resp.json()["auth_url"]

        payload = read_pin(settings, client.cookies.get("pensieve_pin"), max_age=600)
        assert payload == {"pin_id": 987654, "code": "browsercode1"}
    finally:
        client.__exit__(None, None, None)


def test_login_body_never_mints_a_pin_of_its_own(tmp_path):
    """Nothing is called upstream: the browser already did that part."""
    seen: list[httpx.Request] = []

    def _recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _route(request)

    client, _settings = _make_client(tmp_path, _recording)
    try:
        assert client.post(
            "/api/auth/login", json={"pin_id": 1, "code": "abc"}
        ).status_code == 200
        assert seen == []
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "body",
    [
        {"pin_id": 0, "code": "abc"},
        {"pin_id": -1, "code": "abc"},
        {"pin_id": 2**53, "code": "abc"},
        {"pin_id": "abc", "code": "abc"},
        {"pin_id": 111},
        {"code": "abc"},
        {"pin_id": 111, "code": ""},
        {"pin_id": 111, "code": "a" * 129},
        # The code is interpolated into the auth URL's query string, so
        # anything that could append or truncate a parameter is refused.
        {"pin_id": 111, "code": "abc&forwardUrl=https://evil.test"},
        {"pin_id": 111, "code": "abc#frag"},
        {"pin_id": 111, "code": "ab c"},
        {"pin_id": 111, "code": "abc", "extra": "field"},
    ],
)
def test_login_body_rejects_malformed_input(tmp_path, body):
    client, _settings = _make_client(tmp_path)
    try:
        resp = client.post("/api/auth/login", json=body)
        assert resp.status_code == 422
        assert client.cookies.get("pensieve_pin") is None
    finally:
        client.__exit__(None, None, None)


def test_login_body_is_rate_limited_like_the_get_path(tmp_path):
    """Not a cheaper way to grind at the login flow."""
    client, _settings = _make_client(tmp_path)
    try:
        for _ in range(10):
            assert client.post(
                "/api/auth/login", json={"pin_id": 111, "code": "abcd"}
            ).status_code == 200
        assert client.post(
            "/api/auth/login", json={"pin_id": 111, "code": "abcd"}
        ).status_code == 429
    finally:
        client.__exit__(None, None, None)


def test_browser_minted_pin_completes_the_callback(tmp_path):
    """End to end on the new path: POST the PIN, then the callback signs in."""
    client, _settings = _make_client(tmp_path)
    try:
        assert client.post(
            "/api/auth/login", json={"pin_id": 111, "code": "abcd"}
        ).status_code == 200
        resp = client.get("/auth/callback", follow_redirects=False)
        assert resp.status_code == 307
        assert client.cookies.get("pensieve_session") is not None
    finally:
        client.__exit__(None, None, None)


def test_callback_refuses_a_pin_whose_code_is_not_ours(tmp_path):
    """Somebody else's in-flight PIN id is not a way into their account.

    Every the app browser polls under the same client identifier, so a
    guessed PIN id would otherwise be pollable from an attacker's session
    and hand them the victim's token. The 25-character code never leaves the
    browser that minted it.
    """
    client, _settings = _make_client(tmp_path)
    try:
        # The victim's PIN id (111), claimed with a code the attacker guessed.
        assert client.post(
            "/api/auth/login", json={"pin_id": 111, "code": "guessed"}
        ).status_code == 200
        resp = client.get("/auth/callback", follow_redirects=False)
        assert resp.status_code == 400
        assert client.cookies.get("pensieve_session") is None
    finally:
        client.__exit__(None, None, None)
