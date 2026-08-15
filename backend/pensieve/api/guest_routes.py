"""Request-access routes for accounts that are NOT on the server (v0.2.0).

The only unauthenticated write surface in the app. Everything here hangs off
the short-lived ``pensieve_guest`` cookie the denied-login branch sets: a
signed statement that plex.tv just vouched for this account id, name, and
email. It is not a session and grants nothing -- these two routes can create
and read *that account's own* access request, and that is the entire blast
radius.

Because it's unauthenticated, both routes ride the same per-IP limiter as
``/api/auth/login``, and every string that reaches the database or Discord is
length-bounded here rather than trusted from the cookie -- they reach the
owner's notification as well as the database.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from pensieve import notify
from pensieve.auth import GUEST_COOKIE, read_guest
from pensieve.clients.base import CachedHTTP
from pensieve.config import Settings
from pensieve.db import get_db
from pensieve.ratelimit import auth_limiter

router = APIRouter(prefix="/api/guest")

#: Defensive bounds on cookie-sourced strings before they hit the DB/Discord.
_MAX_NAME = 120
_MAX_EMAIL = 254

_INSERT_ACCESS_REQUEST = """
INSERT INTO access_requests (plex_account_id, name, email, state, created_at)
VALUES (?, ?, ?, 'pending', ?)
"""

_INSERT_EVENT = "INSERT INTO events (at, actor, action, detail) VALUES (?, ?, ?, ?)"


def _client_ip(request: Request) -> str:
    """Best-effort client identifier for rate limiting."""
    return request.client.host if request.client else "unknown"


def guest_identity(request: Request) -> dict[str, Any]:
    """FastAPI dependency: the denied account behind the guest cookie.

    Also applies the login rate limiter, so both guest routes are covered by
    exactly one gate rather than each remembering to call it.

    Raises:
        HTTPException: 429 over the per-IP rate limit; 401 if the
            ``pensieve_guest`` cookie is missing, tampered with, expired, or
            not guest-shaped.

    Returns:
        ``{"id": int, "name": str, "email": str}`` with the strings bounded.
    """
    if not auth_limiter.check(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many attempts")

    settings: Settings = request.app.state.settings
    cookie = request.cookies.get(GUEST_COOKIE)
    payload = read_guest(settings, cookie) if cookie else None
    if payload is None or not isinstance(payload.get("id"), int):
        raise HTTPException(status_code=401, detail="Sign in with Plex first")

    return {
        "id": payload["id"],
        "name": str(payload.get("name") or "")[:_MAX_NAME],
        "email": str(payload.get("email") or "")[:_MAX_EMAIL],
    }


@router.post("/access-requests", response_model=None)
async def post_access_request(
    request: Request,
    guest: dict[str, Any] = Depends(guest_identity),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """File (or re-read) this account's request to be let onto the server.

    Idempotent by ``plex_account_id``: a second press of the button reports
    the existing row rather than filing a duplicate or re-pinging Discord --
    the ``UNIQUE`` constraint means an insert would fail anyway, and nagging
    is exactly what an eager tap would otherwise do.

    Returns:
        201 ``{"state": "pending"}`` for a newly filed request (plus an
        events row and one owner notification -- push, or Discord if that
        does not land). 200 ``{"state": "pending"}``
        or ``{"state": "approved"}`` if one already exists. 409 ``{"error":
        "access was declined"}`` if it was denied -- the answer was no, and
        re-asking is not a button. 400 if the Plex account exposes no email,
        since the share API has nothing to invite.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    now = datetime.now(timezone.utc)

    if not guest["email"]:
        return JSONResponse({"error": "no email on plex account"}, status_code=400)

    row = db.execute(
        "SELECT state FROM access_requests WHERE plex_account_id = ?", (guest["id"],)
    ).fetchone()
    if row is not None:
        if row["state"] == "denied":
            return JSONResponse({"error": "access was declined"}, status_code=409)
        return {"state": row["state"]}

    try:
        db.execute(
            _INSERT_ACCESS_REQUEST,
            (guest["id"], guest["name"], guest["email"], now.isoformat()),
        )
    except sqlite3.IntegrityError:
        # A double-tap racing itself: the UNIQUE constraint already did the
        # deduping, so report the row that won rather than 500 at the user.
        row = db.execute(
            "SELECT state FROM access_requests WHERE plex_account_id = ?", (guest["id"],)
        ).fetchone()
        return {"state": row["state"] if row else "pending"}
    db.execute(
        _INSERT_EVENT,
        (now.isoformat(), guest["name"], "access_requested", guest["email"]),
    )

    await notify.owner_event(
        http, db, settings,
        title="🚪 Access request",
        body=f"{guest['name']} ({guest['email']}) wants in",
    )
    return JSONResponse({"state": "pending"}, status_code=201)


@router.get("/access-requests/me", response_model=None)
async def get_my_access_request(
    guest: dict[str, Any] = Depends(guest_identity),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Where this account's request stands, if it has one.

    Returns:
        ``{"state": "none" | "pending" | "approved" | "denied"}``.
    """
    row = db.execute(
        "SELECT state FROM access_requests WHERE plex_account_id = ?", (guest["id"],)
    ).fetchone()
    return {"state": row["state"] if row else "none"}
