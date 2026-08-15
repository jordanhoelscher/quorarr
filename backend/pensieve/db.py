"""SQLite database connection and schema management."""
import sqlite3
from pathlib import Path
from typing import Iterator

from fastapi import Request

from pensieve.migrations import migrate


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


def init_db(conn: sqlite3.Connection) -> None:
    """Bring the database up to the current schema version (idempotent).

    Thin wrapper over :func:`pensieve.migrations.migrate`, kept as the name
    every caller already uses. Safe to call on a fresh file, on a database
    this build already migrated, and on one created before migrations existed.
    Raises if a migration fails -- see the module docstring in
    ``pensieve/migrations.py``.
    """
    migrate(conn)


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
