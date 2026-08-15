"""SQLite database connection and schema management."""
import sqlite3
from pathlib import Path
from typing import Iterator

from fastapi import Request


def connect(db_path: str) -> sqlite3.Connection:
    """Connect to SQLite database with WAL mode and foreign keys enabled.

    Creates parent directory if it doesn't exist. Sets row_factory to sqlite3.Row
    for dict-like row access, enables WAL mode for concurrency, enables foreign
    key constraints, and sets isolation_level to None (autocommit mode).
    """
    # Auto-create parent directory
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # Connect with check_same_thread=False for use in FastAPI
    conn = sqlite3.connect(db_path, check_same_thread=False)

    # Enable dict-like row access
    conn.row_factory = sqlite3.Row

    # Enable WAL mode for concurrent access
    conn.execute("PRAGMA journal_mode=WAL")

    # Enable foreign key constraints
    conn.execute("PRAGMA foreign_keys=ON")

    # Autocommit mode (no implicit transactions)
    conn.isolation_level = None

    return conn


SCHEMA = """
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


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables (idempotent).

    Executes the schema DDL. CREATE TABLE IF NOT EXISTS makes this safe
    to call multiple times.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing table, so it
    will not add ``users.revoked`` to a database created before that column
    existed. Nothing is deployed yet, so instead of migration machinery
    there's a single best-effort ALTER: it succeeds once on a pre-existing
    dev DB and raises ``OperationalError`` ("duplicate column name") on
    every run after that, which is the expected steady state.
    """
    conn.executescript(SCHEMA)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency that provides a database connection per request.

    Yields a connection opened from request.app.state.settings.db_path
    and closes it in a finally block.
    """
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()
