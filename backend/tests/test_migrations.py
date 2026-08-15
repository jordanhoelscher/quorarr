"""Schema-migration tests: fresh DBs, pre-versioning DBs, and failure atomicity."""
import sqlite3

import pytest

from pensieve import migrations
from pensieve.db import connect, init_db
from pensieve.migrations import LATEST_VERSION, migrate

#: Every table the current schema is expected to carry.
EXPECTED_TABLES = {
    "users", "deletion_flags", "quality_requests", "events",
    "access_requests", "title_hints", "discover_4k_requests",
    "push_subscriptions",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_db_migrates_to_latest(tmp_path):
    """An empty file gets the whole schema and the current version stamp."""
    conn = connect(str(tmp_path / "fresh.db"))
    assert _user_version(conn) == 0

    migrate(conn)

    assert EXPECTED_TABLES <= _tables(conn)
    assert "revoked" in _columns(conn, "users")
    assert _user_version(conn) == LATEST_VERSION
    assert LATEST_VERSION > 0


def test_migrate_is_idempotent(tmp_path):
    """A second run applies nothing and touches no data."""
    conn = connect(str(tmp_path / "twice.db"))
    migrate(conn)
    conn.execute(
        "INSERT INTO users (plex_account_id, name, role, last_seen)"
        " VALUES (1, 'Sam', 'member', 'now')")

    migrate(conn)

    assert _user_version(conn) == LATEST_VERSION
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_pre_versioning_db_adopts_baseline_without_data_loss(tmp_path):
    """A 0.1.0-shaped DB (no ``revoked``, no later tables, user_version 0).

    This is the deployed-before-versioning case: the migration runner has to
    adopt it in place, add the missing column, create the missing tables, and
    leave every existing row alone.
    """
    path = str(tmp_path / "old.db")
    conn = connect(path)
    conn.executescript("""
        CREATE TABLE users (
            plex_account_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('owner','member')),
            last_seen TEXT NOT NULL
        );
        CREATE TABLE deletion_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT NOT NULL CHECK(media_type IN ('movie','series')),
            arr_id INTEGER NOT NULL,
            season_number INTEGER,
            title TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            state TEXT NOT NULL DEFAULT 'flagged',
            flagged_by INTEGER NOT NULL,
            flagged_by_name TEXT NOT NULL,
            flagged_at TEXT NOT NULL,
            vetoed_by_name TEXT,
            resolved_at TEXT,
            error TEXT,
            note TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT
        );
    """)
    conn.execute(
        "INSERT INTO users (plex_account_id, name, role, last_seen)"
        " VALUES (7, 'Sam', 'owner', '2026-01-01T00:00:00Z')")
    conn.execute(
        "INSERT INTO deletion_flags (media_type, arr_id, title, flagged_by,"
        " flagged_by_name, flagged_at)"
        " VALUES ('movie', 42, 'Dune', 7, 'Sam', '2026-01-01T00:00:00Z')")
    assert _user_version(conn) == 0

    migrate(conn)

    assert _user_version(conn) == LATEST_VERSION
    assert EXPECTED_TABLES <= _tables(conn)
    assert "revoked" in _columns(conn, "users")

    row = conn.execute("SELECT * FROM users WHERE plex_account_id = 7").fetchone()
    assert row["name"] == "Sam"
    assert row["role"] == "owner"
    assert row["last_seen"] == "2026-01-01T00:00:00Z"
    assert row["revoked"] == 0
    flag = conn.execute("SELECT * FROM deletion_flags").fetchone()
    assert flag["title"] == "Dune"


def test_pre_versioning_db_with_revoked_already_present(tmp_path):
    """The live 0.9.1 shape: everything there, only the version stamp missing."""
    path = str(tmp_path / "live.db")
    conn = connect(path)
    migrate(conn)
    conn.execute(
        "INSERT INTO users (plex_account_id, name, role, last_seen, revoked)"
        " VALUES (7, 'Sam', 'owner', 'now', 1)")
    # Rewind the stamp to simulate a DB built by a pre-migrations release.
    conn.execute("PRAGMA user_version = 0")

    migrate(conn)

    assert _user_version(conn) == LATEST_VERSION
    assert conn.execute(
        "SELECT revoked FROM users WHERE plex_account_id = 7").fetchone()[0] == 1


def test_failing_migration_raises_and_leaves_version_unchanged(tmp_path, monkeypatch):
    """A broken migration must abort loudly and roll back its whole step."""
    conn = connect(str(tmp_path / "bad.db"))
    migrate(conn)
    baseline_version = _user_version(conn)

    bad = (baseline_version + 1, """
        CREATE TABLE half_applied (id INTEGER PRIMARY KEY);
        THIS IS NOT SQL;
    """)
    monkeypatch.setattr(
        migrations, "MIGRATIONS", migrations.MIGRATIONS + [bad])

    with pytest.raises(sqlite3.Error):
        migrate(conn)

    assert _user_version(conn) == baseline_version
    assert "half_applied" not in _tables(conn)
    # The connection is usable afterwards -- the failed step rolled back
    # rather than leaving a write transaction open.
    conn.execute("SELECT COUNT(*) FROM users").fetchone()


def test_migration_versions_are_ordered_and_unique():
    versions = [version for version, _ in migrations.MIGRATIONS]
    assert versions == sorted(set(versions))
    assert versions[0] == 1


def test_init_db_stamps_the_version(tmp_path):
    """``init_db`` is still the public entry point and now runs migrations."""
    conn = connect(str(tmp_path / "init.db"))
    init_db(conn)
    assert _user_version(conn) == LATEST_VERSION
    assert EXPECTED_TABLES <= _tables(conn)


def test_newer_db_is_left_alone(tmp_path, caplog):
    """A DB stamped by a future release is not downgraded or re-migrated."""
    conn = connect(str(tmp_path / "future.db"))
    migrate(conn)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 5}")

    migrate(conn)

    assert _user_version(conn) == LATEST_VERSION + 5
