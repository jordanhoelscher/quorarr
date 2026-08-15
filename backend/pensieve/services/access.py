"""Keeping the ``users`` table honest against who Plex actually shares with.

Since 0.5.2 the login gate admits an account on the strength of an un-revoked
``users`` row *or* a Plex share, because an owner approval has to work before
the friend accepts the invite email. The cost of that is real: on its own it
means pulling someone's library share in Plex no longer shuts them out of
the dashboard. This module is the other half — the hourly sweep walks the
membership against plex.tv and revokes anyone Plex no longer knows about.

Two rules shape everything here:

**Revocation only goes one way.** This never un-revokes. Re-admitting someone
is an owner action (approving their request, which clears the flag). If a
sweep restored anyone it happened to find in Plex, a deliberate cut-off would
quietly undo itself on the next tick.

**"I could not read the share list" must never mean "nobody has a share."**
The plex.tv readers raise rather than return an empty set, and the caller
skips the tick entirely on failure. An empty set reaching ``revoke_unshared``
is a real answer — the owner shares with nobody — and will revoke the whole
membership, which is correct only when it is genuinely true.
"""

import sqlite3
from datetime import datetime

from pensieve.clients import plex_tv
from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.config import Settings

_CANDIDATES = """
SELECT plex_account_id, name FROM users
WHERE revoked = 0 AND role != 'owner' AND plex_account_id != ?
"""

_REVOKE = "UPDATE users SET revoked = 1 WHERE plex_account_id = ?"

_INSERT_EVENT = "INSERT INTO events (at, actor, action, detail) VALUES (?, ?, ?, ?)"


def revoke_unshared(
    conn: sqlite3.Connection,
    entitled: set[int],
    owner_account_id: int,
    now: datetime,
) -> list[dict]:
    """Revoke every member Plex no longer vouches for.

    Args:
        conn: Open database connection.
        entitled: Plex account ids that may stay — the union of accounts with
            an **accepted** share and accounts with an invite still pending.
            The union matters: an approved member who hasn't opened the Plex
            email yet appears only in the second list, and treating that as
            "no share" would revoke them about an hour after approval.
        owner_account_id: Skipped unconditionally. The owner owns the server
            and so is never in either plex.tv list; without this the sweep
            would lock them out of their own dashboard on the first tick.
        now: Timestamp for the audit rows.

    Returns:
        One ``{"plex_account_id": int, "name": str}`` per account revoked on
        *this* call. Already-revoked rows are not re-reported, so a caller
        that notifies the owner tells them once rather than every hour.
    """
    doomed = [
        dict(row)
        for row in conn.execute(_CANDIDATES, (owner_account_id,)).fetchall()
        if row["plex_account_id"] not in entitled
    ]

    for user in doomed:
        conn.execute(_REVOKE, (user["plex_account_id"],))
        conn.execute(
            _INSERT_EVENT,
            (
                now.isoformat(),
                "system",
                "access_revoked",
                f"{user['name']} — no longer shared on Plex",
            ),
        )

    return doomed


async def share_state(
    http: CachedHTTP, settings: Settings, plex_account_id: int
) -> str:
    """Where a member stands with the Plex server itself.

    Since 0.5.2 an owner approval alone gets someone in, so "is a
    member" and "is on the Plex server" can disagree. They can browse
    everything and still have no Jellyseerr user, which makes every request
    fail — this is the read that lets the app say *why*.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        plex_account_id: The member's Plex account id.

    Returns:
        ``"active"`` — accepted share, can request.
        ``"pending"`` — invite sent, not yet accepted.
        ``"none"`` — in neither list.
        ``"unknown"`` — plex.tv could not be read.

    Never raises. This value only ever *explains* a failure or decorates a
    view, so an upstream outage must degrade to saying nothing rather than
    taking down the caller with it.
    """
    try:
        if plex_account_id in await plex_tv.list_shared_account_ids(http, settings):
            return "active"
        if plex_account_id in await plex_tv.list_pending_invite_account_ids(http, settings):
            return "pending"
    except UpstreamError:
        return "unknown"
    return "none"
