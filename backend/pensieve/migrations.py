"""Versioned schema migrations, stamped in ``PRAGMA user_version``.

The database is its own version register: SQLite keeps a 32-bit integer in the
file header that nothing else touches, so a Quorarr container can open any
deployment's ``pensieve.db``, see how far behind it is, and bring it forward
without a migrations table, a tool, or a human.

**Adding a migration:** append ``(next_version, sql)`` to :data:`MIGRATIONS`.
Never edit a migration that has shipped -- a deployment that already ran it
will not run it again, so an edit only changes what *new* installs get, which
is how two databases claiming the same version end up with different shapes.

Migration 1 is the baseline: the complete schema as of 0.9.1, written so it is
a no-op against a database that already has that shape. Every statement is
``IF NOT EXISTS``, and the one column that predates ``IF NOT EXISTS``'s reach
(``users.revoked``, added to a table that already existed) is handled by
:func:`_add_missing_columns`, which asks ``PRAGMA table_info`` rather than
firing an ``ALTER`` and swallowing the error. That is what lets a database
created before this module existed -- ``user_version`` 0, tables present, rows
in them -- adopt the baseline in place instead of being rebuilt.
"""
import logging
import sqlite3

logger = logging.getLogger(__name__)

#: Baseline: the complete schema as of 0.9.1. Idempotent against both an empty
#: file and a live pre-versioning database.
_BASELINE = """
CREATE TABLE IF NOT EXISTS users (
    plex_account_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','member')),
    last_seen TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deletion_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL CHECK(media_type IN ('movie','series')),
    arr_id INTEGER NOT NULL,
    season_number INTEGER,
    title TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    state TEXT NOT NULL DEFAULT 'flagged' CHECK(state IN ('flagged','vetoed','pending_approval','approved','denied','executed')),
    flagged_by INTEGER NOT NULL,
    flagged_by_name TEXT NOT NULL,
    flagged_at TEXT NOT NULL,
    vetoed_by_name TEXT,
    resolved_at TEXT,
    error TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS quality_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL,
    arr_id INTEGER NOT NULL,
    season_number INTEGER,
    title TEXT NOT NULL,
    current_quality TEXT,
    requested_quality TEXT NOT NULL CHECK(requested_quality IN ('1080p','4K')),
    state TEXT NOT NULL CHECK(state IN ('auto_triggered','pending_approval','approved','denied','error')),
    requested_by INTEGER NOT NULL,
    requested_by_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    note TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_account_id INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','denied')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS title_hints (
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    PRIMARY KEY (media_type, tmdb_id)
);

CREATE TABLE IF NOT EXISTS discover_4k_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    seasons_json TEXT,
    requested_by INTEGER NOT NULL,
    requested_by_name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','denied')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    note TEXT
);

-- One pending 4K ask per title, enforced by the database rather than by the
-- route's SELECT-then-INSERT (which two simultaneous requests can both pass).
-- Partial, so it constrains only the pending state: a title can be asked for
-- again once an earlier ask has been approved or denied. The route catches
-- the IntegrityError and answers the same 409 the pre-check does.
CREATE UNIQUE INDEX IF NOT EXISTS idx_discover_4k_pending
    ON discover_4k_requests (media_type, tmdb_id) WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_account_id INTEGER NOT NULL,
    endpoint TEXT NOT NULL UNIQUE,
    keys_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

#: ``(target_version, sql)`` in ascending order. Append only.
MIGRATIONS: list[tuple[int, str]] = [
    (1, _BASELINE),
]

#: Columns the baseline's ``CREATE TABLE IF NOT EXISTS`` cannot reach on a
#: database whose table already exists, as ``version -> {table: {column: ddl}}``.
#: ``users.revoked`` shipped after ``users`` did, so a database created before
#: it has the table without the column.
_ADDED_COLUMNS: dict[int, dict[str, dict[str, str]]] = {
    1: {"users": {"revoked": "INTEGER NOT NULL DEFAULT 0"}},
}


def latest_version() -> int:
    """Return the version the current code expects."""
    return MIGRATIONS[-1][0] if MIGRATIONS else 0


#: The version a fully-migrated database carries.
LATEST_VERSION = latest_version()


def _add_missing_columns(conn: sqlite3.Connection, version: int) -> None:
    """``ALTER TABLE ... ADD COLUMN`` for columns this migration expects.

    Asks ``PRAGMA table_info`` first, so a column that is already there is
    skipped rather than attempted-and-ignored. Runs inside the migration's
    transaction; any genuine failure propagates and rolls the step back.
    """
    for table, columns in _ADDED_COLUMNS.get(version, {}).items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:  # table not created yet on this DB -- baseline made it
            continue
        for column, ddl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                logger.info("migration %s: added %s.%s", version, table, column)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring ``conn``'s database up to :data:`LATEST_VERSION`.

    Applies every pending migration in order, each in its own
    ``BEGIN IMMEDIATE`` transaction that also stamps ``PRAGMA user_version``
    -- so a step either lands whole or not at all, and an interrupted upgrade
    resumes from the last version that actually committed.

    Raises whatever SQLite raises. There is no ``except: pass`` here on
    purpose: a schema that did not apply is a broken deployment, and the
    process should refuse to serve rather than discover it one query at a time.

    Args:
        conn: An open connection, in autocommit mode (``isolation_level=None``,
            which :func:`pensieve.db.connect` sets). Transaction control here
            is explicit.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > LATEST_VERSION:
        logger.warning(
            "database schema version %s is newer than this build expects (%s) "
            "-- leaving it alone", current, LATEST_VERSION)
        return

    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        try:
            # executescript performs no implicit transaction control beyond
            # committing a pending one first (there is none in autocommit
            # mode), so the BEGIN below is still open when it returns -- the
            # python step and the version stamp join the same transaction.
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}")
            _add_missing_columns(conn, version)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            # Guarded: if the BEGIN itself failed (a locked database), there is
            # no transaction to roll back and an unguarded ROLLBACK would raise
            # over the top of the real error.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            logger.exception("migration %s failed; database left at version %s",
                             version, current)
            raise
        current = version
        logger.info("applied migration %s", version)
