"""Web Push (VAPID) delivery to the PWA.

Best-effort by construction, exactly like ``notify.post_discord``: a dead
endpoint, a revoked permission, or a push-service outage must never propagate
into the user-facing action that triggered the notification. Every send is
wrapped and logged; callers get back a *delivered count* and nothing else, and
that count is what ``notify.owner_event`` uses to decide whether Discord still
needs to hear about it.

``pywebpush`` is synchronous (it encrypts and POSTs through ``requests``), so
each send goes off the event loop via ``run_in_threadpool``.

Payload convention -- this is the contract with ``frontend/public/sw.js``::

    {"title": str, "body": str, "tab": "approvals" | "flagged" | "pipeline"}

``tab`` is the app tab the notification opens when tapped.

Subscriptions are keyed by ``endpoint`` (UNIQUE), not by account: one browser
profile owns exactly one endpoint, and a shared device where a second person
signs in re-registers *that same* endpoint, which must move to the new account
rather than fan a stranger's notifications out to both.
"""
import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from pywebpush import WebPushException, webpush
from starlette.concurrency import run_in_threadpool

from pensieve.config import Settings

logger = logging.getLogger(__name__)

#: Push-service responses that mean "this subscription is gone for good".
#: Everything else (500s, timeouts, network errors) is treated as transient --
#: pruning on those would silently unsubscribe people during an outage.
_EXPIRED_STATUSES = frozenset({404, 410})

_UPSERT_SUBSCRIPTION = """
INSERT INTO push_subscriptions (plex_account_id, endpoint, keys_json, created_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(endpoint) DO UPDATE SET
    plex_account_id = excluded.plex_account_id,
    keys_json = excluded.keys_json,
    created_at = excluded.created_at
"""

_SELECT_FOR_USER = """
SELECT endpoint, keys_json FROM push_subscriptions
WHERE plex_account_id = ? ORDER BY id
"""


async def subscribe(
    conn: sqlite3.Connection, *, user_id: int, subscription: dict[str, Any],
    now: datetime,
) -> None:
    """Store (or refresh) a browser's push subscription for an account.

    Args:
        conn: Open database connection.
        user_id: ``users.plex_account_id`` the subscription belongs to.
        subscription: The browser's ``PushSubscription`` JSON --
            ``{"endpoint": str, "keys": {"p256dh": str, "auth": str}}``.
            Shape is validated at the route boundary, not here.
        now: Current time, injected for determinism.
    """
    conn.execute(
        _UPSERT_SUBSCRIPTION,
        (user_id, subscription["endpoint"], json.dumps(subscription["keys"]),
         now.isoformat()),
    )


async def unsubscribe(
    conn: sqlite3.Connection, endpoint: str, *, plex_account_id: int | None = None,
) -> None:
    """Drop a subscription by endpoint. A miss is a no-op, not an error.

    Args:
        conn: Open database connection.
        endpoint: The push endpoint to forget.
        plex_account_id: When given, the delete is additionally scoped to that
            account. The unsubscribe route always passes it, so knowing
            somebody else's endpoint URL cannot mute their notifications.
    """
    if plex_account_id is None:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        return
    conn.execute(
        "DELETE FROM push_subscriptions WHERE endpoint = ? AND plex_account_id = ?",
        (endpoint, plex_account_id),
    )


def has_subscriptions(conn: sqlite3.Connection, plex_account_id: int) -> bool:
    """Whether an account has at least one registered push endpoint."""
    row = conn.execute(
        "SELECT 1 FROM push_subscriptions WHERE plex_account_id = ? LIMIT 1",
        (plex_account_id,),
    ).fetchone()
    return row is not None


def subscribed_account_ids(
    conn: sqlite3.Connection, *, exclude: int | None = None
) -> list[int]:
    """Every account with at least one push endpoint, optionally minus one."""
    rows = conn.execute(
        "SELECT DISTINCT plex_account_id FROM push_subscriptions ORDER BY plex_account_id"
    ).fetchall()
    return [r["plex_account_id"] for r in rows if r["plex_account_id"] != exclude]


async def send_to_user(
    conn: sqlite3.Connection, settings: Settings, plex_account_id: int,
    payload: dict[str, Any],
) -> int:
    """Push ``payload`` to every endpoint registered for one account.

    Never raises. A ``404``/``410`` from the push service deletes that
    subscription row (the browser dropped it or the app was uninstalled);
    anything else is logged and skipped, leaving the row for the next attempt.

    Args:
        conn: Open database connection.
        settings: App settings, for the VAPID keypair and subject.
        plex_account_id: Account to notify.
        payload: ``{"title", "body", "tab"}`` -- see the module docstring.

    Returns:
        How many endpoints accepted the push. ``0`` when push is unconfigured
        (empty VAPID keys), when the account has no subscriptions, or when
        every send failed -- callers treat that as "this person was not told".
    """
    if not settings.vapid_private_key or not settings.vapid_public_key:
        return 0

    rows = conn.execute(_SELECT_FOR_USER, (plex_account_id,)).fetchall()
    data = json.dumps(payload)
    delivered = 0

    for row in rows:
        endpoint = row["endpoint"]
        try:
            keys = json.loads(row["keys_json"])
        except ValueError:
            logger.warning("push subscription has unreadable keys; skipping")
            continue

        try:
            await run_in_threadpool(
                webpush,
                subscription_info={"endpoint": endpoint, "keys": keys},
                data=data,
                vapid_private_key=settings.vapid_private_key,
                # A fresh dict per endpoint on purpose: pywebpush *mutates*
                # this, stamping in the `aud` of the first endpoint it sees.
                # A shared dict would sign every later push in a fan-out for
                # the wrong audience, and those get rejected.
                vapid_claims={"sub": settings.vapid_subject},
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in _EXPIRED_STATUSES:
                conn.execute(
                    "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
                )
                logger.info("pruned expired push subscription (HTTP %s)", status)
            else:
                logger.warning("web push failed (HTTP %s)", status, exc_info=True)
            continue
        except Exception:  # noqa: BLE001 -- delivery must never fail the action
            logger.warning("web push failed", exc_info=True)
            continue

        delivered += 1

    return delivered


async def broadcast(
    conn: sqlite3.Connection, settings: Settings, payload: dict[str, Any],
    *, exclude: int | None = None,
) -> int:
    """Push ``payload`` to every subscribed account, optionally minus one.

    Used for events the whole household cares about (a new deletion flag),
    where the person who caused it does not need telling.

    Returns:
        Total endpoints that accepted the push, across all accounts.
    """
    delivered = 0
    for account_id in subscribed_account_ids(conn, exclude=exclude):
        delivered += await send_to_user(conn, settings, account_id, payload)
    return delivered
