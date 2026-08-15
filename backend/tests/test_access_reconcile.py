"""Plex share reconciliation: losing the library share loses the dashboard too.

The counterpart to the 0.5.2 login gate. That change made an owner approval
sufficient on its own, which also meant un-sharing in Plex no longer shut
anyone out. These tests pin the other half: the sweep walks the ``users``
table against what plex.tv actually reports and revokes anyone who has
neither an accepted share nor an invite still in flight.
"""

from datetime import UTC, datetime

import httpx
import pytest

from pensieve.clients import plex_tv
from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.config import Settings
from pensieve.db import connect, init_db
from pensieve.services import access
from tests.conftest import make_settings, seed_user

NOW = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)

_SHARED_SERVERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer friendlyName="myPlex" size="2">
<SharedServer id="10000001" username="alex" email="g@example.com" userID="700000001" name="Example Plex"/>
<SharedServer id="10000002" username="robin" email="l@example.com" userID="700000002" name="Example Plex"/>
</MediaContainer>"""

_INVITES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer friendlyName="myPlex" size="1">
  <Invite id="700000003" createdAt="1700000000" friend="1" server="1" username="morgan" email="w@example.com">
    <Server name="Example Plex" numLibraries="4"/>
  </Invite>
</MediaContainer>"""


def _db(settings: Settings):
    conn = connect(settings.db_path)
    init_db(conn)
    return conn


# --------------------------------------------------------------- parsing


def _client(handler) -> CachedHTTP:
    return CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_list_shared_account_ids_reads_the_userid_attribute(tmp_path):
    settings = make_settings(tmp_path)
    http = _client(lambda r: httpx.Response(200, text=_SHARED_SERVERS_XML))

    assert await plex_tv.list_shared_account_ids(http, settings) == {700000001, 700000002}


async def test_list_pending_invite_account_ids_reads_the_invite_id(tmp_path):
    """The Invite's own ``id`` *is* the plex account id (verified live)."""
    settings = make_settings(tmp_path)
    http = _client(lambda r: httpx.Response(200, text=_INVITES_XML))

    assert await plex_tv.list_pending_invite_account_ids(http, settings) == {700000003}


async def test_share_list_rejects_unparseable_xml_rather_than_reporting_nobody(tmp_path):
    """An empty set here would revoke the entire membership. It must raise."""
    settings = make_settings(tmp_path)
    http = _client(lambda r: httpx.Response(200, text="<html>gateway error</html>"))

    with pytest.raises(UpstreamError):
        await plex_tv.list_shared_account_ids(http, settings)


# ---------------------------------------------------------- reconciliation


def test_revokes_a_member_whose_plex_share_was_removed(tmp_path):
    settings = make_settings(tmp_path, plex_owner_account_id=1)
    conn = _db(settings)
    seed_user(settings, user_id=500, name="Gone")

    revoked = access.revoke_unshared(conn, {700000002}, settings.plex_owner_account_id, NOW)

    assert [r["plex_account_id"] for r in revoked] == [500]
    row = conn.execute("SELECT revoked FROM users WHERE plex_account_id=500").fetchone()
    assert row["revoked"] == 1


def test_leaves_a_member_who_still_has_the_share(tmp_path):
    settings = make_settings(tmp_path, plex_owner_account_id=1)
    conn = _db(settings)
    seed_user(settings, user_id=700000002, name="Robin")

    assert access.revoke_unshared(conn, {700000002}, 1, NOW) == []
    row = conn.execute("SELECT revoked FROM users WHERE plex_account_id=700000002").fetchone()
    assert row["revoked"] == 0


def test_spares_an_approved_member_whose_invite_is_still_pending(tmp_path):
    """The 0.5.2 case. They have no share *yet*; that is not the same as lost.

    Without this the sweep would revoke the very person the previous release
    fixed, roughly an hour after the owner approved them.
    """
    settings = make_settings(tmp_path, plex_owner_account_id=1)
    conn = _db(settings)
    seed_user(settings, user_id=700000003, name="morgan")

    # The caller unions accepted shares with pending invites before calling.
    assert access.revoke_unshared(conn, {700000002, 700000003}, 1, NOW) == []
    row = conn.execute("SELECT revoked FROM users WHERE plex_account_id=700000003").fetchone()
    assert row["revoked"] == 0


def test_never_revokes_the_owner_who_is_in_neither_plex_list(tmp_path):
    """The owner owns the server, so they are never a shared-with account."""
    settings = make_settings(tmp_path, plex_owner_account_id=700000009)
    conn = _db(settings)
    seed_user(settings, user_id=700000009, name="Ada", role="owner")

    assert access.revoke_unshared(conn, {700000002}, 700000009, NOW) == []
    row = conn.execute("SELECT revoked FROM users WHERE plex_account_id=700000009").fetchone()
    assert row["revoked"] == 0


def test_skips_an_owner_role_row_even_if_the_id_does_not_match_settings(tmp_path):
    """Defence in depth: a misconfigured owner id must not lock the owner out."""
    settings = make_settings(tmp_path, plex_owner_account_id=1)
    conn = _db(settings)
    seed_user(settings, user_id=999, name="Ada", role="owner")

    assert access.revoke_unshared(conn, set(), 1, NOW) == []
    row = conn.execute("SELECT revoked FROM users WHERE plex_account_id=999").fetchone()
    assert row["revoked"] == 0


def test_is_idempotent_so_the_owner_is_told_once_not_every_hour(tmp_path):
    settings = make_settings(tmp_path, plex_owner_account_id=1)
    conn = _db(settings)
    seed_user(settings, user_id=500, name="Gone")

    assert len(access.revoke_unshared(conn, set(), 1, NOW)) == 1
    assert access.revoke_unshared(conn, set(), 1, NOW) == []


def test_a_manual_revocation_is_not_undone_by_a_still_present_share(tmp_path):
    """Revocation only ever goes one way here.

    Re-admitting someone is an owner action (approving their request, which
    un-revokes). If this function un-revoked anyone it found in Plex, it
    would quietly overturn a deliberate cut-off every hour.
    """
    settings = make_settings(tmp_path, plex_owner_account_id=1)
    conn = _db(settings)
    seed_user(settings, user_id=700000002, name="Robin", revoked=1)

    assert access.revoke_unshared(conn, {700000002}, 1, NOW) == []
    row = conn.execute("SELECT revoked FROM users WHERE plex_account_id=700000002").fetchone()
    assert row["revoked"] == 1


def test_records_an_event_for_each_revocation(tmp_path):
    settings = make_settings(tmp_path, plex_owner_account_id=1)
    conn = _db(settings)
    seed_user(settings, user_id=500, name="Gone")

    access.revoke_unshared(conn, set(), 1, NOW)

    row = conn.execute(
        "SELECT actor, action, detail FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["action"] == "access_revoked"
    assert "Gone" in row["detail"]


# ------------------------------------------------------- sweep integration


def _plex_routes(shared_xml: str = _SHARED_SERVERS_XML, invites_xml: str = _INVITES_XML):
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/shared_servers"):
            return httpx.Response(200, text=shared_xml)
        if request.url.path == "/api/invites/requested":
            return httpx.Response(200, text=invites_xml)
        return httpx.Response(404)

    return route


async def test_sweep_revokes_the_unshared_and_spares_the_invited(tmp_path):
    from pensieve.main import reconcile_shares_once

    settings = make_settings(tmp_path, plex_owner_account_id=700000009)
    conn = _db(settings)
    conn.close()
    seed_user(settings, user_id=700000009, name="Ada", role="owner")
    seed_user(settings, user_id=700000002, name="Robin")        # accepted share
    seed_user(settings, user_id=700000003, name="morgan")      # invite pending
    seed_user(settings, user_id=500, name="Gone")             # neither

    revoked = await reconcile_shares_once(settings, _client(_plex_routes()))

    assert [r["plex_account_id"] for r in revoked] == [500]

    conn = connect(settings.db_path)
    state = {
        r["plex_account_id"]: r["revoked"]
        for r in conn.execute("SELECT plex_account_id, revoked FROM users")
    }
    conn.close()
    assert state == {700000009: 0, 700000002: 0, 700000003: 0, 500: 1}


async def test_sweep_revokes_nobody_when_plex_is_unreachable(tmp_path):
    """The failure mode that matters: an outage must not empty the house."""
    from pensieve.main import reconcile_shares_once

    settings = make_settings(tmp_path, plex_owner_account_id=700000009)
    conn = _db(settings)
    conn.close()
    seed_user(settings, user_id=700000002, name="Robin")
    seed_user(settings, user_id=500, name="Gone")

    http = _client(lambda r: httpx.Response(503))
    assert await reconcile_shares_once(settings, http) == []

    conn = connect(settings.db_path)
    revoked = [r["revoked"] for r in conn.execute("SELECT revoked FROM users")]
    conn.close()
    assert revoked == [0, 0]


async def test_sweep_revokes_nobody_when_plex_answers_a_non_mediacontainer(tmp_path):
    """A well-formed HTML error page parses fine and lists no shares."""
    from pensieve.main import reconcile_shares_once

    settings = make_settings(tmp_path, plex_owner_account_id=700000009)
    conn = _db(settings)
    conn.close()
    seed_user(settings, user_id=500, name="Gone")

    http = _client(_plex_routes(shared_xml="<html>502 Bad Gateway</html>"))
    assert await reconcile_shares_once(settings, http) == []

    conn = connect(settings.db_path)
    assert conn.execute("SELECT revoked FROM users").fetchone()["revoked"] == 0
    conn.close()


async def test_sweep_skips_the_tick_if_only_the_invite_list_fails(tmp_path):
    """Half a picture is the dangerous one: shares alone would revoke invitees."""
    from pensieve.main import reconcile_shares_once

    settings = make_settings(tmp_path, plex_owner_account_id=700000009)
    conn = _db(settings)
    conn.close()
    seed_user(settings, user_id=700000003, name="morgan")

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/shared_servers"):
            return httpx.Response(200, text=_SHARED_SERVERS_XML)
        return httpx.Response(500)

    assert await reconcile_shares_once(settings, _client(route)) == []

    conn = connect(settings.db_path)
    assert conn.execute("SELECT revoked FROM users").fetchone()["revoked"] == 0
    conn.close()
