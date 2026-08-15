import sqlite3

from pensieve.db import connect, init_db


def test_schema_creates_all_tables(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    init_db(conn)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "deletion_flags", "quality_requests",
            "events", "push_subscriptions"} <= names


def test_init_db_is_idempotent(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    init_db(conn)
    init_db(conn)  # must not raise


def test_flag_state_check_constraint(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    init_db(conn)
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO deletion_flags (media_type, arr_id, title, state,"
            " flagged_by, flagged_by_name, flagged_at)"
            " VALUES ('movie', 1, 'X', 'bogus', 1, 'j', 'now')")
