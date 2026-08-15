"""Deletion-flag state machine.

Pure SQL-backed logic operating on a ``sqlite3.Connection`` and an injected
``now: datetime``. No HTTP, no wall-clock reads — callers pass the current
time so behavior stays deterministic and testable.

State machine:
    flagged --(sweep_expired, > VETO_WINDOW_DAYS)--> pending_approval
    flagged --(veto_flag)--> vetoed
    pending_approval --(resolve_flag)--> approved | denied
    approved --(resolve_flag, 'denied' only)--> denied
    approved --(mark_executed)--> executed
    approved --(mark_error)--> approved (with error set)

A flag scope is (media_type, arr_id, season_number), with NULL season_number
treated as its own distinct scope value (whole-series / movie-level flags).
"""
import sqlite3
from datetime import datetime, timedelta

VETO_WINDOW_DAYS = 14
REFLAG_COOLDOWN_DAYS = 30

_ACTIVE_STATES = ("flagged", "pending_approval", "approved")
_COOLDOWN_STATES = ("vetoed", "denied")


class FlagError(Exception):
    """Raised when a deletion-flag state transition is invalid."""


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def _fetch_flag(conn: sqlite3.Connection, flag_id: int) -> sqlite3.Row:
    """Fetch a deletion_flags row by id, raising FlagError if missing."""
    row = conn.execute(
        "SELECT * FROM deletion_flags WHERE id = ?", (flag_id,)
    ).fetchone()
    if row is None:
        raise FlagError(f"no deletion flag with id {flag_id}")
    return row


def _log_event(
    conn: sqlite3.Connection, *, at: datetime, actor: str, action: str,
    detail: str | None = None,
) -> None:
    """Append a row to the events table."""
    conn.execute(
        "INSERT INTO events (at, actor, action, detail) VALUES (?, ?, ?, ?)",
        (at.isoformat(), actor, action, detail),
    )


def create_flag(
    conn: sqlite3.Connection, *, media_type: str, arr_id: int,
    season_number: int | None, title: str, size_bytes: int,
    reason: str | None, by_id: int, by_name: str, now: datetime,
) -> dict:
    """Create a new deletion flag for a media scope.

    Raises FlagError if an active flag (flagged/pending_approval/approved)
    already exists for the same (media_type, arr_id, season_number) scope,
    or if a vetoed/denied flag for that scope was resolved less than
    REFLAG_COOLDOWN_DAYS ago.
    """
    active_placeholders = ",".join("?" for _ in _ACTIVE_STATES)
    active = conn.execute(
        "SELECT id FROM deletion_flags WHERE media_type = ? AND arr_id = ?"
        " AND IFNULL(season_number, -1) = IFNULL(?, -1)"
        f" AND state IN ({active_placeholders})",
        (media_type, arr_id, season_number, *_ACTIVE_STATES),
    ).fetchone()
    if active is not None:
        raise FlagError(
            f"an active deletion flag already exists for {media_type} "
            f"{arr_id} season {season_number}"
        )

    cooldown_cutoff = now - timedelta(days=REFLAG_COOLDOWN_DAYS)
    cooldown_placeholders = ",".join("?" for _ in _COOLDOWN_STATES)
    recent = conn.execute(
        "SELECT id FROM deletion_flags WHERE media_type = ? AND arr_id = ?"
        " AND IFNULL(season_number, -1) = IFNULL(?, -1)"
        f" AND state IN ({cooldown_placeholders}) AND resolved_at > ?",
        (media_type, arr_id, season_number, *_COOLDOWN_STATES,
         cooldown_cutoff.isoformat()),
    ).fetchone()
    if recent is not None:
        raise FlagError(
            f"{media_type} {arr_id} season {season_number} was vetoed/denied"
            f" within the last {REFLAG_COOLDOWN_DAYS} days"
        )

    cur = conn.execute(
        "INSERT INTO deletion_flags (media_type, arr_id, season_number,"
        " title, size_bytes, reason, state, flagged_by, flagged_by_name,"
        " flagged_at) VALUES (?, ?, ?, ?, ?, ?, 'flagged', ?, ?, ?)",
        (media_type, arr_id, season_number, title, size_bytes, reason,
         by_id, by_name, now.isoformat()),
    )
    flag_id = cur.lastrowid
    _log_event(
        conn, at=now, actor=by_name, action="flag_created",
        detail=f"{media_type} {arr_id} season {season_number}: {title}",
    )
    return _row_to_dict(_fetch_flag(conn, flag_id))


def veto_flag(
    conn: sqlite3.Connection, flag_id: int, by_name: str, now: datetime,
) -> dict:
    """Veto a deletion flag, closing it out. Only valid from 'flagged'."""
    row = _fetch_flag(conn, flag_id)
    if row["state"] != "flagged":
        raise FlagError(
            f"flag {flag_id} is in state {row['state']!r}, not 'flagged'"
        )

    conn.execute(
        "UPDATE deletion_flags SET state = 'vetoed', vetoed_by_name = ?,"
        " resolved_at = ? WHERE id = ?",
        (by_name, now.isoformat(), flag_id),
    )
    _log_event(conn, at=now, actor=by_name, action="flag_vetoed",
               detail=f"flag {flag_id}")
    return _row_to_dict(_fetch_flag(conn, flag_id))


def sweep_expired(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    """Move every 'flagged' row older than VETO_WINDOW_DAYS to 'pending_approval'.

    Returns the list of moved rows (as dicts).
    """
    cutoff = now - timedelta(days=VETO_WINDOW_DAYS)
    rows = conn.execute(
        "SELECT id FROM deletion_flags WHERE state = 'flagged'"
        " AND flagged_at < ?",
        (cutoff.isoformat(),),
    ).fetchall()
    moved = []
    for row in rows:
        flag_id = row["id"]
        conn.execute(
            "UPDATE deletion_flags SET state = 'pending_approval'"
            " WHERE id = ?",
            (flag_id,),
        )
        _log_event(conn, at=now, actor="system", action="flag_swept",
                   detail=f"flag {flag_id}")
        moved.append(_row_to_dict(_fetch_flag(conn, flag_id)))
    return moved


#: Source states each resolution accepts. 'denied' also accepts 'approved'
#: so a flag whose execution keeps failing (e.g. the movie was already
#: removed from Radarr by hand, so every DELETE 404s) has a terminal exit.
#: Without it the only permitted transition is retry-forever: the row parks
#: in the owner's queue, shows "Approved" to every member for a title that
#: was never deleted, and blocks re-flagging that scope, escapable only by
#: hand-editing SQLite.
_RESOLVE_SOURCES = {
    "approved": ("pending_approval",),
    "denied": ("pending_approval", "approved"),
}


def resolve_flag(
    conn: sqlite3.Connection, flag_id: int, state: str,
    note: str | None, now: datetime,
) -> dict:
    """Resolve a flag to 'approved' or 'denied'.

    `state` must be 'approved' or 'denied'. Approving requires a
    'pending_approval' source state; denying also accepts 'approved' (see
    `_RESOLVE_SOURCES`).
    """
    allowed_from = _RESOLVE_SOURCES.get(state)
    if allowed_from is None:
        raise FlagError(f"invalid resolution state {state!r}")

    row = _fetch_flag(conn, flag_id)
    if row["state"] not in allowed_from:
        raise FlagError(
            f"flag {flag_id} is in state {row['state']!r}, not "
            + " or ".join(repr(s) for s in allowed_from)
        )

    conn.execute(
        "UPDATE deletion_flags SET state = ?, note = ?, resolved_at = ?"
        " WHERE id = ?",
        (state, note, now.isoformat(), flag_id),
    )
    _log_event(conn, at=now, actor="owner", action="flag_resolved",
               detail=f"flag {flag_id} -> {state}")
    return _row_to_dict(_fetch_flag(conn, flag_id))


def mark_executed(conn: sqlite3.Connection, flag_id: int, now: datetime) -> dict:
    """Mark an approved flag as executed, clearing any prior error."""
    row = _fetch_flag(conn, flag_id)
    if row["state"] != "approved":
        raise FlagError(
            f"flag {flag_id} is in state {row['state']!r}, not 'approved'"
        )

    conn.execute(
        "UPDATE deletion_flags SET state = 'executed', error = NULL"
        " WHERE id = ?",
        (flag_id,),
    )
    _log_event(conn, at=now, actor="system", action="flag_executed",
               detail=f"flag {flag_id}")
    return _row_to_dict(_fetch_flag(conn, flag_id))


def mark_error(conn: sqlite3.Connection, flag_id: int, error: str) -> None:
    """Record an execution error on a flag without changing its state.

    Only valid from 'approved' (the state mark_executed's caller — the
    execution pipeline — operates from).
    """
    row = _fetch_flag(conn, flag_id)
    if row["state"] != "approved":
        raise FlagError(
            f"flag {flag_id} is in state {row['state']!r}, not 'approved'"
        )
    conn.execute(
        "UPDATE deletion_flags SET error = ? WHERE id = ?",
        (error, flag_id),
    )


def list_flags(
    conn: sqlite3.Connection, states: list[str] | None = None,
) -> list[dict]:
    """List deletion flags, newest first, optionally filtered by state."""
    if states:
        placeholders = ",".join("?" for _ in states)
        rows = conn.execute(
            f"SELECT * FROM deletion_flags WHERE state IN ({placeholders})"
            " ORDER BY flagged_at DESC, id DESC",
            states,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM deletion_flags ORDER BY flagged_at DESC, id DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
