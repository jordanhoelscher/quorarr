"""Plex OAuth login: PIN handshake, session issuance, role gate wiring.

Flow: the caller obtains a plex.tv PIN, hands it here, and gets back the
hosted auth URL with the PIN remembered in a short-lived signed cookie. Plex
redirects the browser to ``GET /auth/callback`` once the user authenticates;
that route polls the PIN for the resulting token, checks the account actually
has a share on our server, and — if so — upserts the user and sets the
long-lived session cookie. If it doesn't, the browser goes back to the SPA
with ``?denied=1`` plus a short-lived guest cookie, which is the entry point
for the request-access flow in ``api/guest_routes.py``.

**Who mints the PIN matters to the person signing in.** When the *server*
mints it (the original ``GET /api/auth/login``), plex.tv records the PIN
against this app's datacentre IP, and Plex then emails the friend a "Security
Alert — a new device signed in" naming an address nowhere near them. Nothing
is wrong, but it reads as a breach. So since 0.5.1 the browser mints its own
PIN against plex.tv directly (CORS is open there, verified live) and hands
``{pin_id, code}`` to ``POST /api/auth/login``; the PIN is then created from
the friend's own address. The GET path stays as the fallback for a browser
whose cross-origin call fails.

Accepting a *caller-supplied* PIN id needs one guard the server-minted path
got for free. plex.tv only lets a PIN be polled by the client identifier that
created it — but every browser here shares one client identifier, so a
bare PIN id would let anyone who guessed an in-flight id poll it from their
own session and harvest somebody else's token. The signed cookie therefore
carries the ``code`` as well, and the callback refuses a PIN whose code does
not match. The code is 25 random characters minted by plex.tv and never
leaves the owning browser, which puts the handshake back where it was.
"""

import sqlite3
from datetime import UTC, datetime

# Bound as a module-level name (rather than calling asyncio.sleep directly)
# so tests can monkeypatch just this poll-retry delay via
# `pensieve.api.auth_routes._sleep`, without mutating the shared `asyncio`
# module -- a global patch there would also zero out unrelated sleeps
# elsewhere in the app (e.g. main.py's hourly sweep loop).
from asyncio import sleep as _sleep

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from pensieve.auth import (
    GUEST_COOKIE,
    PIN_COOKIE,
    SESSION_COOKIE,
    current_user,
    read_pin,
    sign_guest,
    sign_pin,
    sign_session,
)
from pensieve.clients import plex_tv
from pensieve.clients.base import CachedHTTP
from pensieve.config import Settings
from pensieve.db import get_db
from pensieve.ratelimit import auth_limiter

router = APIRouter()

_PIN_MAX_AGE_SECONDS = 600
_SESSION_MAX_AGE_SECONDS = 30 * 86400
_GUEST_MAX_AGE_SECONDS = 900
_POLL_ATTEMPTS = 5
_POLL_DELAY_SECONDS = 1.0

_UPSERT_USER = """
INSERT INTO users (plex_account_id, name, role, last_seen)
VALUES (?, ?, ?, ?)
ON CONFLICT(plex_account_id) DO UPDATE SET
    name = excluded.name,
    role = excluded.role,
    last_seen = excluded.last_seen
"""


def _client_ip(request: Request) -> str:
    """Best-effort client identifier for rate limiting."""
    return request.client.host if request.client else "unknown"


class LoginBody(BaseModel):
    """A PIN the browser minted at plex.tv itself.

    Both fields are bounded because both are attacker-supplied: this endpoint
    is unauthenticated by nature. ``code`` is additionally pinned to
    alphanumerics — it is interpolated into the hosted auth URL's query
    string, and a value carrying ``&`` or ``#`` could otherwise append or
    truncate parameters on the page we send someone to.
    """

    model_config = ConfigDict(extra="forbid")

    #: plex.tv PIN ids are positive integers (~1.9e9 today); the ceiling is
    #: JavaScript's safe-integer limit, past which the value cannot have
    #: round-tripped through a browser intact anyway.
    pin_id: int = Field(gt=0, le=2**53 - 1)
    #: 25 characters for a ``strong=true`` PIN; bounded generously in case
    #: plex.tv ever lengthens it.
    code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9]+$")


def _login_response(settings: Settings, *, pin_id: int, code: str) -> JSONResponse:
    """The auth URL for a PIN, with that PIN remembered in a signed cookie.

    The cookie carries the code as well as the id. The callback checks it
    against what plex.tv reports for the PIN, which is what stops a
    caller-supplied id from being someone else's in-flight login (see the
    module docstring).
    """
    response = JSONResponse({"auth_url": plex_tv.auth_url(settings, code)})
    response.set_cookie(
        PIN_COOKIE,
        sign_pin(settings, {"pin_id": pin_id, "code": code}),
        max_age=_PIN_MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.get("/api/auth/client-id")
def client_id(request: Request) -> dict[str, str]:
    """The ``X-Plex-Client-Identifier`` the browser should mint its PIN under.

    Public on purpose: this value is already visible in every hosted auth URL
    the login flow hands out, and plex.tv treats it as an app name rather
    than a secret. It has to match what ``/auth/callback`` polls with, or
    plex.tv answers 404 on the poll.
    """
    settings: Settings = request.app.state.settings
    return {"client_id": settings.plex_client_id}


@router.get("/api/auth/login")
async def login(request: Request) -> JSONResponse:
    """Start the plex.tv PIN auth flow, minting the PIN here.

    The fallback path, kept for a browser whose direct call to plex.tv fails
    (an extension blocking it, a captive portal, a future CORS change). It
    works identically, at the cost of the PIN — and so the "new device"
    address Plex emails the friend about — belonging to the server.

    Rate-limited per client IP (429 over limit).
    """
    if not auth_limiter.check(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many login attempts")

    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    pin = await plex_tv.create_pin(http, settings)
    return _login_response(settings, pin_id=pin["id"], code=pin["code"])


@router.post("/api/auth/login")
async def login_with_browser_pin(request: Request, body: LoginBody) -> JSONResponse:
    """Adopt a PIN the browser already minted, and hand back its auth URL.

    Nothing is verified against plex.tv here — an id and code that mean
    nothing simply produce an auth URL that will not complete, and the
    callback is where a PIN has to prove itself. Rate-limited per client IP
    exactly like the GET path, so this is not a cheaper way to grind at the
    login flow. 422 on a malformed body.
    """
    if not auth_limiter.check(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many login attempts")

    settings: Settings = request.app.state.settings
    return _login_response(settings, pin_id=body.pin_id, code=body.code)


def _is_approved_member(db: sqlite3.Connection, plex_account_id: int) -> bool:
    """Has the owner already let this account in, independently of plex.tv?

    A ``users`` row is only ever written by an owner approving an access
    request, or by a login that already passed the share check — both
    owner-sanctioned. Honouring it here is what makes approval self-sufficient:
    the plex.tv share invite that approval sends is *pending* until the friend
    accepts the email, and ``has_server_access`` reports a share only once
    accepted. Without this check the app calls someone a member in its own
    database and then refuses them at the door, which is exactly what happened
    on 2026-08-14.

    ``revoked`` is honoured, so cutting someone off is still one flag in the
    ``users`` table — the same lever ``auth.current_user`` pulls on every
    request. Note the consequence: un-sharing a library in Plex alone no
    longer locks an existing member out. Revoke them here.
    """
    row = db.execute(
        "SELECT revoked FROM users WHERE plex_account_id = ?", (plex_account_id,)
    ).fetchone()
    return row is not None and not row["revoked"]


def _pin_is_ours(pin_payload: dict, pin: dict) -> bool:
    """Does the PIN plex.tv is describing belong to the browser holding this cookie?

    The PIN id alone does not answer that: since 0.5.1 the id can be supplied
    by the caller, and every browser polls under the same client
    identifier, so an id someone guessed would otherwise be pollable from
    their session. The 25-character code never leaves the browser that minted
    it, so requiring it to match is what makes the id safe to accept.

    A cookie minted before 0.5.1 has no code, and a plex.tv response without
    one cannot be compared against — both are treated as a pass rather than
    locking people out of a login flow that is already half-finished. The
    first case drains within the cookie's ten minutes; the second is plex.tv
    changing its own contract (it returns ``code`` today, verified live).
    """
    expected = pin_payload.get("code")
    actual = pin.get("code")
    if not expected or not isinstance(actual, str):
        return True
    return actual == expected


@router.get("/auth/callback")
async def callback(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Complete the plex.tv PIN flow: verify, upsert the user, start a session.

    Reads the PIN id from the signed ``pensieve_pin`` cookie set by
    ``/api/auth/login``, polls plex.tv for the resulting token (Plex can
    redirect the browser back before the token has propagated, so this
    retries a few times), then checks the account is allowed in: either the
    owner already approved it (an un-revoked ``users`` row) or it has a share
    on our server. Either alone is enough — see ``_is_approved_member`` for
    why the database half is not redundant.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    raw_pin_cookie = request.cookies.get(PIN_COOKIE)
    pin_payload = (
        read_pin(settings, raw_pin_cookie, max_age=_PIN_MAX_AGE_SECONDS)
        if raw_pin_cookie
        else None
    )
    if pin_payload is None:
        raise HTTPException(status_code=400, detail="Missing or expired login attempt")

    token = None
    for attempt in range(_POLL_ATTEMPTS):
        pin = await plex_tv.fetch_pin(http, settings, pin_payload["pin_id"])
        if not _pin_is_ours(pin_payload, pin):
            raise HTTPException(status_code=400, detail="Missing or expired login attempt")
        token = pin.get("authToken")
        if token:
            break
        if attempt < _POLL_ATTEMPTS - 1:
            await _sleep(_POLL_DELAY_SECONDS)

    if not token:
        raise HTTPException(status_code=400, detail="Login was not completed")

    user = await plex_tv.get_user(http, token, settings)
    # The local answer first: it is free, and it means a returning member is
    # not locked out by a plex.tv `/resources` outage. Same verdict either
    # way — this is an `or`, not a new precedence.
    if not _is_approved_member(db, user["id"]) and not await plex_tv.has_server_access(
        http, token, settings
    ):
        # Send them back into the app rather than to a dead-end error page:
        # the SPA's login screen reads `?denied=1` and explains what to do
        # about it, which a bare 403 body cannot. 303 so the browser
        # re-issues as GET (the callback itself is a GET, but 303 states the
        # intent and survives any future method change).
        denied = RedirectResponse("/?denied=1", status_code=303)
        denied.delete_cookie(PIN_COOKIE)
        # A rejected account still proved who it is, so hand it a short-lived
        # guest cookie: that's what lets the login screen offer "Request
        # access" instead of a dead end. It authorizes nothing -- the only
        # routes that read it are the /api/guest pair, which can create or
        # read that account's own access request and nothing else.
        denied.set_cookie(
            GUEST_COOKIE,
            sign_guest(
                settings,
                {"id": user["id"], "name": user["name"], "email": user.get("email") or ""},
            ),
            max_age=_GUEST_MAX_AGE_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return denied

    role = "owner" if user["id"] == settings.plex_owner_account_id else "member"
    db.execute(
        _UPSERT_USER,
        (user["id"], user["name"], role, datetime.now(UTC).isoformat()),
    )

    response = RedirectResponse(url="/")
    response.delete_cookie(PIN_COOKIE)
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(settings, {"id": user["id"], "name": user["name"], "role": role}),
        max_age=_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.get("/api/auth/me")
def me(user: dict = Depends(current_user)) -> dict:
    """Return the current session's user info, or 401 via ``current_user``."""
    return {"id": user["id"], "name": user["name"], "role": user["role"]}


@router.post("/api/auth/logout")
def logout() -> Response:
    """Clear the session cookie."""
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE)
    return response
