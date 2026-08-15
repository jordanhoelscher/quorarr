"""Session cookie signing/verification and the login/role FastAPI dependencies.

Sessions and the short-lived PIN handshake cookie ride on the same
``itsdangerous.URLSafeTimedSerializer`` (keyed off ``settings.session_secret``)
but with **distinct salts** — a validly-signed PIN cookie must never verify
as a session cookie or vice versa, even though they use the same secret key.
Mixing them up isn't just cosmetic: ``current_user`` trusts whatever
``read_session`` hands back, so a payload of the wrong shape (e.g. a PIN
cookie's ``{"pin_id": N}``) would otherwise blow up downstream with a
``KeyError`` instead of a clean 401.
"""

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from pensieve.config import Settings
from pensieve.db import connect

SESSION_COOKIE = "pensieve_session"
PIN_COOKIE = "pensieve_pin"
GUEST_COOKIE = "pensieve_guest"
_SESSION_SALT = "pensieve-session"
_PIN_SALT = "pensieve-pin"
_GUEST_SALT = "pensieve-guest"
_SESSION_MAX_AGE = 30 * 86400
_GUEST_MAX_AGE = 900
_SESSION_KEYS = {"id", "name", "role"}
_GUEST_KEYS = {"id", "name", "email"}


def _serializer(settings: Settings, salt: str) -> URLSafeTimedSerializer:
    """Build the itsdangerous serializer for a given cookie's salt."""
    return URLSafeTimedSerializer(settings.session_secret, salt=salt)


def sign_session(settings: Settings, payload: dict) -> str:
    """Sign a session payload into a cookie-safe, tamper-proof token.

    Args:
        settings: App settings, for ``session_secret``.
        payload: JSON-serializable dict to embed in the token; expected to
            contain ``id``, ``name``, and ``role`` (not enforced here, but
            ``read_session`` will reject anything else).

    Returns:
        The signed, URL-safe token string.
    """
    return _serializer(settings, _SESSION_SALT).dumps(payload)


def read_session(settings: Settings, cookie: str, max_age: int = _SESSION_MAX_AGE) -> dict | None:
    """Verify, decode, and shape-check a signed session cookie value.

    Args:
        settings: App settings, for ``session_secret``.
        cookie: The raw cookie value to verify.
        max_age: Maximum token age in seconds before it's considered expired.

    Returns:
        The decoded payload dict, or None if the signature is invalid, the
        token has expired, or the payload isn't a dict containing ``id``,
        ``name``, and ``role`` (e.g. a PIN cookie signed with a different
        salt would already fail signature verification, but this is a
        defense-in-depth check against any other shape mismatch).
    """
    try:
        payload = _serializer(settings, _SESSION_SALT).loads(cookie, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or not _SESSION_KEYS <= payload.keys():
        return None
    return payload


def sign_pin(settings: Settings, payload: dict) -> str:
    """Sign a PIN-handshake payload into a cookie-safe, tamper-proof token.

    Args:
        settings: App settings, for ``session_secret``.
        payload: JSON-serializable dict to embed in the token, e.g.
            ``{"pin_id": int}``.

    Returns:
        The signed, URL-safe token string.
    """
    return _serializer(settings, _PIN_SALT).dumps(payload)


def read_pin(settings: Settings, cookie: str, max_age: int) -> dict | None:
    """Verify and decode a signed PIN-handshake cookie value.

    Args:
        settings: App settings, for ``session_secret``.
        cookie: The raw cookie value to verify.
        max_age: Maximum token age in seconds before it's considered expired.

    Returns:
        The decoded payload dict, or None if the signature is invalid or the
        token has expired.
    """
    try:
        return _serializer(settings, _PIN_SALT).loads(cookie, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def sign_guest(settings: Settings, payload: dict) -> str:
    """Sign a denied-login identity into a short-lived cookie token.

    This is the *only* thing a rejected account carries into the
    request-access flow, and it is deliberately not a session: it grants no
    access to anything, it expires in 15 minutes, and its own salt means it
    can never verify as a session (or a PIN) cookie.

    Args:
        settings: App settings, for ``session_secret``.
        payload: JSON-serializable dict, expected to contain ``id``,
            ``name``, and ``email``.

    Returns:
        The signed, URL-safe token string.
    """
    return _serializer(settings, _GUEST_SALT).dumps(payload)


def read_guest(
    settings: Settings, cookie: str, max_age: int = _GUEST_MAX_AGE
) -> dict | None:
    """Verify, decode, and shape-check a signed guest cookie value.

    Args:
        settings: App settings, for ``session_secret``.
        cookie: The raw cookie value to verify.
        max_age: Maximum token age in seconds before it's considered expired.

    Returns:
        The decoded payload dict, or None if the signature is invalid, the
        token has expired, or the payload isn't a dict containing ``id``,
        ``name``, and ``email``. The shape check is defense in depth: the
        guest routes index the database on ``id``, so a payload of another
        cookie's shape must fail here rather than downstream.
    """
    try:
        payload = _serializer(settings, _GUEST_SALT).loads(cookie, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or not _GUEST_KEYS <= payload.keys():
        return None
    return payload


def current_user(request: Request) -> dict:
    """FastAPI dependency: the logged-in user, re-checked against the database.

    The signed cookie only establishes *identity*. Authorization is read
    fresh from the ``users`` row on every request, so revoking access (or
    changing a role) takes effect on the next call instead of whenever the
    long-lived cookie happens to expire. Without this, un-sharing someone in
    Plex would leave them with full member access — including flagging media
    for deletion — for the remaining life of their cookie, with no way to cut
    them off short of rotating ``SESSION_SECRET`` and logging out everyone.

    Uses its own short-lived connection rather than the ``get_db`` dependency:
    this runs as a router-level dependency (including on routes that take no
    ``db`` argument at all), so it cannot rely on one being injected.

    Raises:
        HTTPException: 401 if the ``pensieve_session`` cookie is missing,
            tampered with, expired, doesn't decode to a session-shaped
            payload (e.g. a PIN cookie value presented as a session cookie),
            has no matching ``users`` row, or that row is revoked.

    Returns:
        ``{"id": int, "name": str, "role": str}`` — with ``role`` taken from
        the database row, never from the cookie.
    """
    settings: Settings = request.app.state.settings
    cookie = request.cookies.get(SESSION_COOKIE)
    user = read_session(settings, cookie) if cookie else None
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    conn = connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT name, role, revoked FROM users WHERE plex_account_id = ?",
            (user["id"],),
        ).fetchone()
    finally:
        conn.close()

    if row is None or row["revoked"]:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return {"id": user["id"], "name": row["name"], "role": row["role"]}


def require_owner(user: dict = Depends(current_user)) -> dict:
    """FastAPI dependency: the logged-in user, gated to the ``owner`` role.

    Raises:
        HTTPException: 403 if the current user's role is not ``owner``.

    Returns:
        The session payload for the current owner.
    """
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Owner access required")
    return user
