"""Tests for the v0.3.0 Web Push lane.

Three surfaces: the ``pensieve.push`` store/send primitives, the
``notify.owner_event`` push-then-Discord fallback that replaced the three bare
owner webhook calls, and the ``/api/push/*`` routes the PWA subscribes through.

``pywebpush.webpush`` is monkeypatched at ``pensieve.push.webpush`` throughout
-- it is a blocking ``requests`` call to a real push service, and the point of
these tests is the wiring around it (payload shape, VAPID kwargs, per-endpoint
fan-out, expiry pruning), not the encryption it performs.
"""
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pywebpush import WebPushException

from pensieve import notify, push
from pensieve.auth import SESSION_COOKIE, sign_session
from pensieve.clients.base import CachedHTTP
from pensieve.db import connect, init_db
from pensieve.main import create_app
from tests.conftest import make_settings, seed_user

_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _subscription(endpoint: str = "https://push.example.com/ep-1") -> dict:
    """A browser-shaped PushSubscription JSON blob."""
    return {"endpoint": endpoint, "keys": {"p256dh": "p256dh-key", "auth": "auth-key"}}


def _db(settings) -> "object":
    conn = connect(settings.db_path)
    init_db(conn)
    return conn


class _Recorder:
    """Stands in for ``pywebpush.webpush``; records every call, or raises."""

    def __init__(self, raises: dict[str, Exception] | None = None) -> None:
        self.calls: list[dict] = []
        self.raises = raises or {}

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        endpoint = kwargs["subscription_info"]["endpoint"]
        exc = self.raises.get(endpoint)
        if exc is not None:
            raise exc
        return None


def _expired(status: int = 410) -> WebPushException:
    """A WebPushException carrying a response with the given status code."""
    return WebPushException("gone", response=httpx.Response(status))


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


def _login(client: TestClient, settings, *, user_id: int = 2, name: str = "Sam",
           role: str = "member") -> None:
    seed_user(settings, user_id=user_id, name=name, role=role)
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": user_id, "name": name, "role": role})
    )


# --- subscribe / unsubscribe --------------------------------------------------


async def test_subscribe_stores_the_row(tmp_path):
    settings = make_settings(tmp_path)
    conn = _db(settings)
    try:
        await push.subscribe(conn, user_id=7, subscription=_subscription(), now=_NOW)

        row = conn.execute("SELECT * FROM push_subscriptions").fetchone()
        assert row["plex_account_id"] == 7
        assert row["endpoint"] == "https://push.example.com/ep-1"
        assert json.loads(row["keys_json"]) == {"p256dh": "p256dh-key", "auth": "auth-key"}
        assert row["created_at"] == _NOW.isoformat()
    finally:
        conn.close()


async def test_subscribe_same_endpoint_upserts_rather_than_duplicating(tmp_path):
    """A browser re-subscribing (new keys, or a different account on a shared
    device) must land on the one row that ``endpoint`` uniquely identifies --
    otherwise the UNIQUE constraint turns a routine re-subscribe into a 500."""
    settings = make_settings(tmp_path)
    conn = _db(settings)
    try:
        await push.subscribe(conn, user_id=7, subscription=_subscription(), now=_NOW)
        moved = {"endpoint": "https://push.example.com/ep-1",
                 "keys": {"p256dh": "new-p256dh", "auth": "new-auth"}}
        await push.subscribe(conn, user_id=9, subscription=moved, now=_NOW)

        rows = conn.execute("SELECT * FROM push_subscriptions").fetchall()
        assert len(rows) == 1
        assert rows[0]["plex_account_id"] == 9
        assert json.loads(rows[0]["keys_json"])["p256dh"] == "new-p256dh"
    finally:
        conn.close()


async def test_unsubscribe_removes_only_that_endpoint(tmp_path):
    settings = make_settings(tmp_path)
    conn = _db(settings)
    try:
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://p/a"), now=_NOW)
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://p/b"), now=_NOW)

        await push.unsubscribe(conn, "https://p/a")

        rows = conn.execute("SELECT endpoint FROM push_subscriptions").fetchall()
        assert [r["endpoint"] for r in rows] == ["https://p/b"]
    finally:
        conn.close()


def test_has_subscriptions(tmp_path):
    settings = make_settings(tmp_path)
    conn = _db(settings)
    try:
        assert push.has_subscriptions(conn, 7) is False
        conn.execute(
            "INSERT INTO push_subscriptions (plex_account_id, endpoint, keys_json, created_at)"
            " VALUES (7, 'https://p/a', '{}', ?)",
            (_NOW.isoformat(),),
        )
        assert push.has_subscriptions(conn, 7) is True
        assert push.has_subscriptions(conn, 8) is False
    finally:
        conn.close()


# --- send_to_user -------------------------------------------------------------


async def test_send_to_user_calls_webpush_once_per_subscription(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    conn = _db(settings)
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://p/a"), now=_NOW)
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://p/b"), now=_NOW)
        await push.subscribe(conn, user_id=8, subscription=_subscription("https://p/c"), now=_NOW)

        payload = {"title": "Marked for deletion", "body": "Dune — 14 days to veto",
                   "tab": "flagged"}
        delivered = await push.send_to_user(conn, settings, 7, payload)

        assert delivered == 2
        assert [c["subscription_info"]["endpoint"] for c in recorder.calls] == [
            "https://p/a", "https://p/b",
        ]
        call = recorder.calls[0]
        assert json.loads(call["data"]) == payload
        assert call["vapid_private_key"] == settings.vapid_private_key
        assert call["vapid_claims"] == {"sub": settings.vapid_subject}
        assert call["subscription_info"]["keys"] == {"p256dh": "p256dh-key", "auth": "auth-key"}
    finally:
        conn.close()


async def test_send_to_user_passes_a_fresh_claims_dict_per_endpoint(tmp_path, monkeypatch):
    """pywebpush *mutates* vapid_claims, stamping in the ``aud`` of whichever
    endpoint it saw first. Reusing one dict across a fan-out would sign every
    later push for the wrong audience, and those pushes are rejected."""
    settings = make_settings(tmp_path)
    conn = _db(settings)
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://a/x"), now=_NOW)
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://b/y"), now=_NOW)

        await push.send_to_user(conn, settings, 7, {"title": "t", "body": "b", "tab": "flagged"})

        first, second = (c["vapid_claims"] for c in recorder.calls)
        assert first is not second
    finally:
        conn.close()


@pytest.mark.parametrize("status", [404, 410])
async def test_send_to_user_prunes_expired_subscriptions(tmp_path, monkeypatch, status):
    settings = make_settings(tmp_path)
    conn = _db(settings)
    recorder = _Recorder(raises={"https://p/dead": _expired(status)})
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://p/dead"), now=_NOW)
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://p/live"), now=_NOW)

        delivered = await push.send_to_user(
            conn, settings, 7, {"title": "t", "body": "b", "tab": "flagged"}
        )

        assert delivered == 1
        rows = conn.execute("SELECT endpoint FROM push_subscriptions").fetchall()
        assert [r["endpoint"] for r in rows] == ["https://p/live"]
    finally:
        conn.close()


async def test_send_to_user_keeps_the_row_on_a_transient_failure(tmp_path, monkeypatch):
    """A 500 from the push service (or a timeout) is the service's problem, not
    a dead subscription -- pruning on it would silently unsubscribe someone
    during an outage."""
    settings = make_settings(tmp_path)
    conn = _db(settings)
    recorder = _Recorder(raises={"https://p/a": _expired(500)})
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://p/a"), now=_NOW)

        delivered = await push.send_to_user(
            conn, settings, 7, {"title": "t", "body": "b", "tab": "flagged"}
        )

        assert delivered == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM push_subscriptions").fetchone()["n"] == 1
    finally:
        conn.close()


async def test_send_to_user_never_raises_on_an_unexpected_error(tmp_path, monkeypatch):
    """Notification delivery must never fail the action that triggered it."""
    settings = make_settings(tmp_path)
    conn = _db(settings)
    recorder = _Recorder(raises={"https://p/a": RuntimeError("boom")})
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://p/a"), now=_NOW)
        assert await push.send_to_user(conn, settings, 7, {"title": "t", "body": "b"}) == 0
    finally:
        conn.close()


async def test_send_to_user_without_vapid_config_is_a_noop(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, vapid_private_key="", vapid_public_key="")
    conn = _db(settings)
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        await push.subscribe(conn, user_id=7, subscription=_subscription(), now=_NOW)
        assert await push.send_to_user(conn, settings, 7, {"title": "t", "body": "b"}) == 0
        assert recorder.calls == []
    finally:
        conn.close()


async def test_broadcast_skips_the_excluded_account(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    conn = _db(settings)
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        await push.subscribe(conn, user_id=7, subscription=_subscription("https://p/a"), now=_NOW)
        await push.subscribe(conn, user_id=8, subscription=_subscription("https://p/b"), now=_NOW)
        await push.subscribe(conn, user_id=9, subscription=_subscription("https://p/c"), now=_NOW)

        delivered = await push.broadcast(
            conn, settings, {"title": "t", "body": "b", "tab": "flagged"}, exclude=8
        )

        assert delivered == 2
        assert sorted(c["subscription_info"]["endpoint"] for c in recorder.calls) == [
            "https://p/a", "https://p/c",
        ]
    finally:
        conn.close()


# --- notify.owner_event -------------------------------------------------------


async def test_owner_event_prefers_push_and_skips_discord(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, discord_webhook_url="http://discord.test/hook")
    conn = _db(settings)
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    router = _Router({})
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(router)))
    try:
        seed_user(settings, user_id=1, name="Ada", role="owner")
        await push.subscribe(conn, user_id=1, subscription=_subscription(), now=_NOW)

        await notify.owner_event(
            http, conn, settings, title="🚪 Access request", body="Neville wants in"
        )

        assert len(recorder.calls) == 1
        assert json.loads(recorder.calls[0]["data"]) == {
            "title": "🚪 Access request", "body": "Neville wants in", "tab": "approvals",
        }
        assert [r for r in router.requests if r.url.path == "/hook"] == []
    finally:
        conn.close()


async def test_owner_event_falls_back_to_discord_without_subscriptions(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, discord_webhook_url="http://discord.test/hook")
    conn = _db(settings)
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    router = _Router({})
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(router)))
    try:
        seed_user(settings, user_id=1, name="Ada", role="owner")

        await notify.owner_event(
            http, conn, settings, title="🚪 Access request", body="Neville wants in"
        )

        assert recorder.calls == []
        hook = [r for r in router.requests if r.url.path == "/hook"]
        assert len(hook) == 1
        sent = json.loads(hook[0].content)
        assert sent["content"] == "🚪 Access request — Neville wants in"
        assert sent["allowed_mentions"] == {"parse": []}
    finally:
        conn.close()


async def test_owner_event_falls_back_when_every_push_fails(tmp_path, monkeypatch):
    """A subscribed-but-undeliverable owner must still be told. Counting
    deliveries (not subscriptions) is what makes that true."""
    settings = make_settings(tmp_path, discord_webhook_url="http://discord.test/hook")
    conn = _db(settings)
    recorder = _Recorder(raises={"https://push.example.com/ep-1": _expired(410)})
    monkeypatch.setattr(push, "webpush", recorder)
    router = _Router({})
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(router)))
    try:
        seed_user(settings, user_id=1, name="Ada", role="owner")
        await push.subscribe(conn, user_id=1, subscription=_subscription(), now=_NOW)

        await notify.owner_event(http, conn, settings, title="T", body="B")

        assert len([r for r in router.requests if r.url.path == "/hook"]) == 1
    finally:
        conn.close()


async def test_owner_event_with_no_owner_row_still_notifies_discord(tmp_path):
    settings = make_settings(tmp_path, discord_webhook_url="http://discord.test/hook")
    conn = _db(settings)
    router = _Router({})
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(router)))
    try:
        await notify.owner_event(http, conn, settings, title="T", body="B")
        assert len([r for r in router.requests if r.url.path == "/hook"]) == 1
    finally:
        conn.close()


# --- routes -------------------------------------------------------------------


def test_public_key_route_returns_the_configured_key(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        resp = client.get("/api/push/public-key")
        assert resp.status_code == 200
        assert resp.json() == {"key": settings.vapid_public_key}
    finally:
        client.__exit__(None, None, None)


def test_public_key_route_returns_empty_when_unconfigured(tmp_path):
    client, settings, _router = _make_client(tmp_path, {}, vapid_public_key="")
    try:
        _login(client, settings)
        assert client.get("/api/push/public-key").json() == {"key": ""}
    finally:
        client.__exit__(None, None, None)


def test_push_routes_require_a_session(tmp_path):
    client, _settings, _router = _make_client(tmp_path, {})
    try:
        assert client.get("/api/push/public-key").status_code == 401
        assert client.post(
            "/api/push/subscribe", json={"subscription": _subscription()}
        ).status_code == 401
        assert client.post(
            "/api/push/unsubscribe", json={"endpoint": "https://p/a"}
        ).status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_subscribe_route_stores_row_and_logs_event(tmp_path):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        resp = client.post("/api/push/subscribe", json={"subscription": _subscription()})
        assert resp.status_code == 201
        assert resp.json() == {"ok": True}

        conn = connect(settings.db_path)
        row = conn.execute("SELECT * FROM push_subscriptions").fetchone()
        events = conn.execute("SELECT * FROM events WHERE action = 'push_subscribed'").fetchall()
        conn.close()
        assert row["plex_account_id"] == 2
        assert row["endpoint"] == "https://push.example.com/ep-1"
        assert len(events) == 1
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"subscription": {}},
        {"subscription": {"endpoint": "https://p/a"}},
        {"subscription": {"endpoint": "https://p/a", "keys": {"p256dh": "x"}}},
        {"subscription": {"endpoint": "", "keys": {"p256dh": "x", "auth": "y"}}},
        # Not an https push endpoint: the server POSTs to whatever it stores,
        # so an authenticated member must not be able to aim it at the LAN.
        {"subscription": {"endpoint": "http://192.168.10.11:9090/x",
                          "keys": {"p256dh": "x", "auth": "y"}}},
        {"subscription": {"endpoint": "https://p/" + "a" * 5000,
                          "keys": {"p256dh": "x", "auth": "y"}}},
    ],
)
def test_subscribe_route_rejects_malformed_bodies(tmp_path, body):
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        assert client.post("/api/push/subscribe", json=body).status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_unsubscribe_route_only_removes_your_own_endpoint(tmp_path):
    """Endpoints are bearer-ish secrets, but the delete is still scoped to the
    session account -- knowing someone else's endpoint must not let you mute
    their notifications."""
    client, settings, _router = _make_client(tmp_path, {})
    try:
        _login(client, settings)
        conn = connect(settings.db_path)
        conn.execute(
            "INSERT INTO push_subscriptions (plex_account_id, endpoint, keys_json, created_at)"
            " VALUES (99, 'https://p/other', '{}', ?)",
            (_NOW.isoformat(),),
        )
        conn.close()

        client.post("/api/push/subscribe", json={"subscription": _subscription()})

        assert client.post(
            "/api/push/unsubscribe", json={"endpoint": "https://p/other"}
        ).status_code == 200
        assert client.post(
            "/api/push/unsubscribe", json={"endpoint": "https://push.example.com/ep-1"}
        ).status_code == 200

        conn = connect(settings.db_path)
        rows = conn.execute("SELECT endpoint FROM push_subscriptions").fetchall()
        conn.close()
        assert [r["endpoint"] for r in rows] == ["https://p/other"]
    finally:
        client.__exit__(None, None, None)


# --- member-facing push events ------------------------------------------------


_MOVIE_42 = {"id": 42, "title": "Old Movie", "qualityProfileId": 6, "sizeOnDisk": 5_000}


def test_flag_create_pushes_to_everyone_but_the_flagger(tmp_path, monkeypatch):
    routes = {("GET", "/api/v3/movie/42"): httpx.Response(200, json=_MOVIE_42)}
    client, settings, _router = _make_client(tmp_path, routes)
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        _login(client, settings, user_id=2, name="Sam")
        seed_user(settings, user_id=3, name="Ron")

        conn = connect(settings.db_path)
        for account_id, endpoint in ((2, "https://p/sam"), (3, "https://p/ron")):
            conn.execute(
                "INSERT INTO push_subscriptions (plex_account_id, endpoint, keys_json,"
                " created_at) VALUES (?, ?, ?, ?)",
                (account_id, endpoint, json.dumps({"p256dh": "x", "auth": "y"}),
                 _NOW.isoformat()),
            )
        conn.close()

        resp = client.post("/api/flags", json={"media_type": "movie", "arr_id": 42})
        assert resp.status_code == 201

        assert [c["subscription_info"]["endpoint"] for c in recorder.calls] == ["https://p/ron"]
        payload = json.loads(recorder.calls[0]["data"])
        assert payload["title"] == "Marked for deletion"
        assert payload["body"] == "Old Movie — 14 days to veto"
        assert payload["tab"] == "flagged"
    finally:
        client.__exit__(None, None, None)


def _insert_quality_request(settings, *, state: str = "pending_approval") -> int:
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        cur = conn.execute(
            "INSERT INTO quality_requests (media_type, arr_id, season_number, title,"
            " current_quality, requested_quality, state, requested_by, requested_by_name,"
            " created_at) VALUES ('movie', 102, NULL, 'Dune', '1080p', '4K', ?, 3, 'Ron', ?)",
            (state, _NOW.isoformat()),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _subscribe_row(settings, account_id: int, endpoint: str) -> None:
    conn = connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO push_subscriptions (plex_account_id, endpoint, keys_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (account_id, endpoint, json.dumps({"p256dh": "x", "auth": "y"}), _NOW.isoformat()),
        )
    finally:
        conn.close()


def test_quality_approve_pushes_the_requester(tmp_path, monkeypatch):
    movie_raw = {"id": 102, "title": "Dune", "qualityProfileId": 3}
    routes = {
        ("GET", "/api/v3/movie/102"): httpx.Response(200, json=movie_raw),
        ("PUT", "/api/v3/movie/102"): httpx.Response(200, json=movie_raw),
        ("POST", "/api/v3/command"): httpx.Response(200, json={"id": 1}),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        _login(client, settings, user_id=1, name="Ada", role="owner")
        seed_user(settings, user_id=3, name="Ron")
        req_id = _insert_quality_request(settings)
        _subscribe_row(settings, 3, "https://p/ron")

        resp = client.post(f"/api/admin/quality/{req_id}/approve")
        assert resp.status_code == 200

        assert [c["subscription_info"]["endpoint"] for c in recorder.calls] == ["https://p/ron"]
        payload = json.loads(recorder.calls[0]["data"])
        assert payload["title"] == "Request approved"
        assert "Dune" in payload["body"]
        assert payload["tab"] == "flagged"
    finally:
        client.__exit__(None, None, None)


def test_quality_deny_pushes_the_requester(tmp_path, monkeypatch):
    client, settings, _router = _make_client(tmp_path, {})
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        _login(client, settings, user_id=1, name="Ada", role="owner")
        seed_user(settings, user_id=3, name="Ron")
        req_id = _insert_quality_request(settings)
        _subscribe_row(settings, 3, "https://p/ron")

        resp = client.post(f"/api/admin/quality/{req_id}/deny", json={"note": "no room"})
        assert resp.status_code == 200

        payload = json.loads(recorder.calls[0]["data"])
        assert payload["title"] == "Request declined"
        assert "Dune" in payload["body"]
    finally:
        client.__exit__(None, None, None)


def test_quality_approve_failure_does_not_push(tmp_path, monkeypatch):
    """The requester learns nothing until something actually happened."""
    routes = {
        ("GET", "/api/v3/movie/102"): httpx.Response(
            200, json={"id": 102, "title": "Dune", "qualityProfileId": 3}
        ),
        ("PUT", "/api/v3/movie/102"): httpx.Response(500, text="boom"),
    }
    client, settings, _router = _make_client(tmp_path, routes)
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        _login(client, settings, user_id=1, name="Ada", role="owner")
        seed_user(settings, user_id=3, name="Ron")
        req_id = _insert_quality_request(settings)
        _subscribe_row(settings, 3, "https://p/ron")

        assert client.post(f"/api/admin/quality/{req_id}/approve").status_code == 502
        assert recorder.calls == []
    finally:
        client.__exit__(None, None, None)


# --- owner notifications now ride owner_event ---------------------------------


def test_4k_request_pushes_the_owner_instead_of_discord(tmp_path, monkeypatch):
    routes = {
        ("GET", "/api/v3/movie/102"): httpx.Response(
            200, json={"id": 102, "title": "Dune", "qualityProfileId": 3, "sizeOnDisk": 5_000}
        ),
    }
    client, settings, router = _make_client(
        tmp_path, routes, discord_webhook_url="http://discord.test/hook"
    )
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        _login(client, settings, user_id=2, name="Sam")
        seed_user(settings, user_id=1, name="Ada", role="owner")
        _subscribe_row(settings, 1, "https://p/ada")

        resp = client.post(
            "/api/quality-requests",
            json={"media_type": "movie", "arr_id": 102, "requested": "4K"},
        )
        assert resp.json()["state"] == "pending_approval"

        assert [r for r in router.requests if r.url.path == "/hook"] == []
        payload = json.loads(recorder.calls[0]["data"])
        assert "Dune" in payload["body"]
        assert "Sam" in payload["body"]
        assert payload["tab"] == "approvals"
    finally:
        client.__exit__(None, None, None)


async def test_sweep_tick_pushes_the_owner_instead_of_discord(tmp_path, monkeypatch):
    """The hourly background tick has no request and no injected connection --
    it opens its own, and must keep it open across the notifications."""
    from datetime import timedelta

    from pensieve.main import run_sweep_once

    settings = make_settings(tmp_path, discord_webhook_url="http://discord.test/hook")
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    seed_user(settings, user_id=1, name="Ada", role="owner")
    _subscribe_row(settings, 1, "https://p/ada")

    conn = connect(settings.db_path)
    conn.execute(
        "INSERT INTO deletion_flags (media_type, arr_id, season_number, title,"
        " size_bytes, reason, state, flagged_by, flagged_by_name, flagged_at)"
        " VALUES ('movie', 555, NULL, 'Old Movie', 1000, NULL, 'flagged', 2, 'Sam', ?)",
        ((datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),),
    )
    conn.close()

    router = _Router({})
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(router)))

    moved = await run_sweep_once(settings, http)

    assert len(moved) == 1
    assert [r for r in router.requests if r.url.path == "/hook"] == []
    payload = json.loads(recorder.calls[0]["data"])
    assert "Old Movie" in payload["body"]
    assert "14 days" in payload["body"]
    assert payload["tab"] == "approvals"


def test_access_request_pushes_the_owner_instead_of_discord(tmp_path, monkeypatch):
    from pensieve.auth import GUEST_COOKIE, sign_guest

    client, settings, router = _make_client(
        tmp_path, {}, discord_webhook_url="http://discord.test/hook"
    )
    recorder = _Recorder()
    monkeypatch.setattr(push, "webpush", recorder)
    try:
        seed_user(settings, user_id=1, name="Ada", role="owner")
        _subscribe_row(settings, 1, "https://p/ada")
        client.cookies.set(
            GUEST_COOKIE,
            sign_guest(settings, {"id": 77, "name": "Neville", "email": "n@example.com"}),
        )

        assert client.post("/api/guest/access-requests").status_code == 201

        assert [r for r in router.requests if r.url.path == "/hook"] == []
        payload = json.loads(recorder.calls[0]["data"])
        assert "Neville" in payload["body"]
        assert payload["tab"] == "approvals"
    finally:
        client.__exit__(None, None, None)
