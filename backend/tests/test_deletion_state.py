from datetime import datetime, timedelta, timezone

import pytest

from pensieve.db import connect, init_db
from pensieve.services import deletion
from pensieve.services.deletion import FlagError

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "p.db"))
    init_db(c)
    return c


def flag(conn, **kw):
    args = dict(media_type="movie", arr_id=42, season_number=None,
                title="Old Movie", size_bytes=5_000, reason=None,
                by_id=2, by_name="Sam", now=NOW)
    args.update(kw)
    return deletion.create_flag(conn, **args)


def test_create_then_duplicate_rejected(conn):
    flag(conn)
    with pytest.raises(FlagError):
        flag(conn, by_id=3, by_name="Alex")


def test_same_movie_different_season_scope_is_distinct(conn):
    flag(conn, media_type="series", arr_id=7, season_number=1)
    flag(conn, media_type="series", arr_id=7, season_number=2)  # ok


def test_veto_closes_flag_and_blocks_reflag_within_cooldown(conn):
    f = flag(conn)
    deletion.veto_flag(conn, f["id"], "Alex", NOW)
    with pytest.raises(FlagError):
        flag(conn, now=NOW + timedelta(days=29))
    flag(conn, now=NOW + timedelta(days=31))  # cooldown over


def test_veto_only_from_flagged(conn):
    f = flag(conn)
    deletion.veto_flag(conn, f["id"], "Alex", NOW)
    with pytest.raises(FlagError):
        deletion.veto_flag(conn, f["id"], "Sam", NOW)


def test_sweep_moves_only_expired(conn):
    old = flag(conn)
    flag(conn, arr_id=99, now=NOW + timedelta(days=10))
    moved = deletion.sweep_expired(conn, NOW + timedelta(days=15))
    assert [m["id"] for m in moved] == [old["id"]]
    assert deletion.list_flags(conn, ["pending_approval"])[0]["id"] == old["id"]


def test_approve_execute_and_error_paths(conn):
    f = flag(conn)
    deletion.sweep_expired(conn, NOW + timedelta(days=15))
    deletion.resolve_flag(conn, f["id"], "approved", None, NOW)
    deletion.mark_error(conn, f["id"], "radarr 500")
    assert deletion.list_flags(conn, ["approved"])[0]["error"] == "radarr 500"
    deletion.mark_executed(conn, f["id"], NOW)
    assert deletion.list_flags(conn, ["executed"])[0]["error"] is None


def test_mark_error_only_from_approved(conn):
    f = flag(conn)
    with pytest.raises(FlagError):
        deletion.mark_error(conn, f["id"], "radarr 500")


def test_deny_allowed_from_approved_but_approve_is_not(conn):
    f = flag(conn)
    deletion.sweep_expired(conn, NOW + timedelta(days=15))
    deletion.resolve_flag(conn, f["id"], "approved", None, NOW)
    deletion.mark_error(conn, f["id"], "radarr 404")

    # Approve-from-approved stays refused (the route's retry path handles it
    # without re-resolving); deny is the terminal escape hatch.
    with pytest.raises(FlagError):
        deletion.resolve_flag(conn, f["id"], "approved", None, NOW)

    denied = deletion.resolve_flag(conn, f["id"], "denied", "giving up", NOW)
    assert denied["state"] == "denied"
    assert denied["note"] == "giving up"


def test_deny_still_refused_from_flagged_and_executed(conn):
    f = flag(conn)
    with pytest.raises(FlagError):
        deletion.resolve_flag(conn, f["id"], "denied", None, NOW)

    deletion.sweep_expired(conn, NOW + timedelta(days=15))
    deletion.resolve_flag(conn, f["id"], "approved", None, NOW)
    deletion.mark_executed(conn, f["id"], NOW)
    with pytest.raises(FlagError):
        deletion.resolve_flag(conn, f["id"], "denied", None, NOW)
