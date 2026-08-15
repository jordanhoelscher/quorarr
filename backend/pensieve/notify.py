"""Owner notifications: Web Push first, Discord as the fallback.

Best-effort only: a notification failure (network error, webhook down,
malformed URL, ...) must never propagate and fail the user-facing action
that triggered it, so every exception is caught and logged here.

``owner_event`` is the front door as of v0.3.0 -- notifications belong in the
app, and Discord is what happens when the app cannot be reached. Every
owner-facing event routes through it rather than calling ``post_discord``
directly, so "did anyone actually get told?" has exactly one answer path.
"""
import logging
import sqlite3

from pensieve import push
from pensieve.clients.base import CachedHTTP
from pensieve.config import Settings

logger = logging.getLogger(__name__)

#: The owner is a single row by construction (one Plex server owner). A
#: revoked owner is skipped: they cannot open the app, so pushing at them
#: would silently swallow the notification -- Discord is the honest channel.
_SELECT_OWNER = """
SELECT plex_account_id FROM users WHERE role = 'owner' AND revoked = 0
ORDER BY plex_account_id LIMIT 1
"""


async def post_discord(http: CachedHTTP, settings: Settings, content: str) -> None:
    """Post a message to the configured Discord webhook, if any.

    No-op when ``settings.discord_webhook_url`` is empty. Any failure
    (missing/invalid URL, connect error, non-2xx response) is caught and
    logged as a warning -- it must never raise. ``content`` embeds
    friend-controlled text (titles), so ``allowed_mentions.parse`` is sent
    empty to suppress any ``@everyone``/``@here``/role-mention injection.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``discord_webhook_url``.
        content: Message text to post.
    """
    if not settings.discord_webhook_url:
        return
    try:
        await http.send_json(
            "POST",
            settings.discord_webhook_url,
            service="discord",
            json={"content": content, "allowed_mentions": {"parse": []}},
        )
    except Exception:
        logger.warning("Discord notification failed", exc_info=True)


async def owner_event(
    http: CachedHTTP, conn: sqlite3.Connection, settings: Settings, *,
    title: str, body: str, tab: str = "approvals",
) -> None:
    """Tell the owner something needs them, preferring an in-app push.

    Falls back to Discord whenever the push did not actually land: no owner
    row, no registered subscriptions, push unconfigured, or every endpoint
    failed. The decision is made on the *delivered count*, not on whether a
    subscription exists -- a stale endpoint would otherwise swallow the only
    notice the owner gets.

    Args:
        http: Shared cached HTTP client, for the Discord fallback.
        conn: Open database connection, for the owner row and subscriptions.
        settings: App settings.
        title: Notification title, e.g. ``"🚪 Access request"``.
        body: Notification body, e.g. ``"Neville (n@example.com) wants in"``.
        tab: Which app tab the notification opens.
    """
    owner = conn.execute(_SELECT_OWNER).fetchone()

    delivered = 0
    if owner is not None and push.has_subscriptions(conn, owner["plex_account_id"]):
        delivered = await push.send_to_user(
            conn, settings, owner["plex_account_id"],
            {"title": title, "body": body, "tab": tab},
        )

    if delivered == 0:
        await post_discord(http, settings, f"{title} — {body}")
