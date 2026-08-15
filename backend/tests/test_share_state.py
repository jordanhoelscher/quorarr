"""A member's Plex share state, and the richer pending-invite list behind it.

0.5.2 let an owner approval alone grant entry, which means "in the app" and
"on the Plex server" can now disagree. This is the read that tells them apart.
"""

import httpx
import pytest

from pensieve.clients import plex_tv
from pensieve.clients.base import CachedHTTP
from pensieve.services import access
from tests.conftest import make_settings

_INVITES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer friendlyName="myPlex" size="2">
  <Invite id="700000003" createdAt="1700000000" friend="1" server="1" username="morgan" email="w@example.com"/>
  <Invite id="700000004" createdAt="1786752147" friend="1" server="1" username="bb5fd3" email="b@example.com"/>
</MediaContainer>"""

_SHARED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer friendlyName="myPlex" size="1">
<SharedServer id="10000002" username="robin" email="l@example.com" userID="700000002" name="Example Plex"/>
</MediaContainer>"""


def _http(handler) -> CachedHTTP:
    return CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _both(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/invites/requested":
        return httpx.Response(200, text=_INVITES_XML)
    if request.url.path.endswith("/shared_servers"):
        return httpx.Response(200, text=_SHARED_XML)
    return httpx.Response(404)


async def test_list_pending_invites_carries_the_fields_the_owner_view_needs(tmp_path):
    settings = make_settings(tmp_path)
    rows = await plex_tv.list_pending_invites(_http(_both), settings)

    assert rows == [
        {"id": 700000003, "username": "morgan", "email": "w@example.com", "invited_at": 1700000000},
        {"id": 700000004, "username": "bb5fd3", "email": "b@example.com", "invited_at": 1786752147},
    ]


async def test_list_pending_invites_drops_rows_missing_an_id(tmp_path):
    """A row with no usable id cannot be matched to a member, so it is noise."""
    xml = """<?xml version="1.0"?><MediaContainer>
      <Invite createdAt="1" username="nope" email="n@example.com"/>
      <Invite id="7" createdAt="2" username="ok" email="o@example.com"/>
    </MediaContainer>"""
    settings = make_settings(tmp_path)
    rows = await plex_tv.list_pending_invites(_http(lambda r: httpx.Response(200, text=xml)), settings)

    assert [row["id"] for row in rows] == [7]


async def test_the_id_set_wrapper_still_answers_what_the_sweep_expects(tmp_path):
    """0.6.0's reconciliation depends on this exact shape; the rewrite must not move it."""
    settings = make_settings(tmp_path)
    ids = await plex_tv.list_pending_invite_account_ids(_http(_both), settings)

    assert ids == {700000003, 700000004}


@pytest.mark.parametrize(
    "account_id,expected",
    [
        (700000002, "active"),     # in shared_servers
        (700000003, "pending"),  # invite sent, not accepted
        (999999, "none"),        # in neither list
    ],
)
async def test_share_state_reads_the_two_lists(tmp_path, account_id, expected):
    settings = make_settings(tmp_path)
    assert await access.share_state(_http(_both), settings, account_id) == expected


async def test_share_state_answers_unknown_rather_than_raising_when_plex_is_down(tmp_path):
    """Diagnostic only. It must never be able to break the caller."""
    settings = make_settings(tmp_path)
    state = await access.share_state(_http(lambda r: httpx.Response(503)), settings, 700000002)

    assert state == "unknown"


async def test_share_state_answers_unknown_on_a_non_mediacontainer_body(tmp_path):
    """A proxy's HTML error page parses as XML; _owner_xml rejects it, we absorb it."""
    settings = make_settings(tmp_path)
    state = await access.share_state(
        _http(lambda r: httpx.Response(200, text="<html>502</html>")), settings, 700000002
    )

    assert state == "unknown"
