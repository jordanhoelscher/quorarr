import json

import httpx
import pytest

from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.clients import plex_tv
from tests.conftest import make_settings


def route(request):
    p = request.url.path
    if p == "/api/v2/pins" and request.method == "POST":
        return httpx.Response(201, json={"id": 111, "code": "abcd"})
    if p == "/api/v2/pins/111":
        return httpx.Response(200, json={"id": 111, "authToken": "tok-1"})
    if p == "/api/v2/user":
        return httpx.Response(200, json={"id": 42, "username": "sam",
                                         "friendlyName": "Sam", "thumb": "t",
                                         "email": "sam@example.com"})
    if p == "/api/v2/resources":
        return httpx.Response(200, json=[
            {"name": "SomeoneElses", "clientIdentifier": "other"},
            {"name": "HomeServer", "clientIdentifier": "machine-123"},
        ])
    return httpx.Response(404)


async def test_full_pin_flow(tmp_path):
    s = make_settings(tmp_path)
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(route)))
    pin = await plex_tv.create_pin(http, s)
    assert pin == {"id": 111, "code": "abcd"}
    url = plex_tv.auth_url(s, pin["code"])
    assert "clientID=pensieve-test" in url and "code=abcd" in url
    assert await plex_tv.poll_pin(http, s, 111) == "tok-1"
    user = await plex_tv.get_user(http, "tok-1", s)
    assert user == {"id": 42, "name": "Sam", "thumb": "t", "email": "sam@example.com"}
    assert await plex_tv.has_server_access(http, "tok-1", s) is True


async def test_access_denied_when_server_not_shared(tmp_path):
    s = make_settings(tmp_path, plex_server_machine_id="not-mine")
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(route)))
    assert await plex_tv.has_server_access(http, "tok-1", s) is False


async def test_access_denied_when_machine_id_is_blank(tmp_path):
    def blank_id(request):
        return httpx.Response(200, json=[
            {"name": "Misconfigured", "clientIdentifier": ""},
        ])
    s = make_settings(tmp_path, plex_server_machine_id="")
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(blank_id)))
    assert await plex_tv.has_server_access(http, "tok-1", s) is False


async def test_unclaimed_pin_returns_none(tmp_path):
    def unclaimed(request):
        return httpx.Response(200, json={"id": 111, "authToken": None})
    s = make_settings(tmp_path)
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(unclaimed)))
    assert await plex_tv.poll_pin(http, s, 111) is None


# --- library sharing (v0.2.0) ------------------------------------------------

_SERVERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer>
  <Server name="HomeServer" machineIdentifier="machine-123">
    <Section id="3" key="1" title="Movies" type="movie"/>
    <Section id="5" key="2" title="TV Shows" type="show"/>
  </Server>
</MediaContainer>
"""


async def test_get_user_falls_back_to_blank_email(tmp_path):
    """A Plex account with no email must not KeyError the login callback."""
    def no_email(request):
        return httpx.Response(200, json={"id": 42, "username": "sam", "thumb": "t"})

    s = make_settings(tmp_path)
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(no_email)))
    assert (await plex_tv.get_user(http, "tok-1", s))["email"] == ""


async def test_list_server_sections_parses_xml(tmp_path):
    s = make_settings(tmp_path, plex_owner_token="ot")
    seen = []

    def servers(request):
        seen.append(request)
        return httpx.Response(200, text=_SERVERS_XML)

    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(servers)))
    assert await plex_tv.list_server_sections(http, s) == [3, 5]
    assert seen[0].url.path == "/api/servers/machine-123"
    assert seen[0].headers["X-Plex-Token"] == "ot"


async def test_list_server_sections_without_owner_token_raises(tmp_path):
    s = make_settings(tmp_path, plex_owner_token="")
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=_SERVERS_XML))))
    with pytest.raises(UpstreamError) as ei:
        await plex_tv.list_server_sections(http, s)
    assert ei.value.service == "plex.tv"


async def test_list_server_sections_rejects_unparseable_body(tmp_path):
    s = make_settings(tmp_path, plex_owner_token="ot")
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="<not xml"))))
    with pytest.raises(UpstreamError):
        await plex_tv.list_server_sections(http, s)


async def test_invite_to_server_posts_the_plexapi_wire_format(tmp_path):
    s = make_settings(tmp_path, plex_owner_token="ot")
    seen = []

    def route(request):
        seen.append(request)
        if request.method == "POST":
            # plex.tv answers the share endpoint with XML, not JSON.
            return httpx.Response(200, text="<MediaContainer/>")
        return httpx.Response(200, text=_SERVERS_XML)

    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(route)))
    assert await plex_tv.invite_to_server(http, s, email="nev@example.com") is None

    post = seen[1]
    assert post.url.path == "/api/servers/machine-123/shared_servers"
    body = json.loads(post.content)
    assert body == {
        "server_id": "machine-123",
        "shared_server": {"library_section_ids": [3, 5], "invited_email": "nev@example.com"},
    }


async def test_invite_to_server_without_owner_token_raises(tmp_path):
    s = make_settings(tmp_path, plex_owner_token="")
    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=_SERVERS_XML))))
    with pytest.raises(UpstreamError):
        await plex_tv.invite_to_server(http, s, email="nev@example.com")


async def test_invite_to_server_propagates_upstream_failure(tmp_path):
    s = make_settings(tmp_path, plex_owner_token="ot")

    def route(request):
        if request.method == "POST":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=_SERVERS_XML)

    http = CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(route)))
    with pytest.raises(UpstreamError) as ei:
        await plex_tv.invite_to_server(http, s, email="nev@example.com")
    assert ei.value.service == "plex.tv"


# ----------------------------------------------------- untrusted XML input

#: The classic "billion laughs" entity bomb, shrunk to four levels so the test
#: stays fast if the defence ever regresses and it actually expands.
_ENTITY_BOMB = """<?xml version="1.0"?>
<!DOCTYPE MediaContainer [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
]>
<MediaContainer><Section id="&c;"/></MediaContainer>"""

_EXTERNAL_ENTITY = """<?xml version="1.0"?>
<!DOCTYPE MediaContainer [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<MediaContainer><Section id="&xxe;"/></MediaContainer>"""


def _owner_http(text: str) -> CachedHTTP:
    return CachedHTTP(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=text)))
    )


async def test_entity_bomb_is_refused_not_expanded(tmp_path):
    """plex.tv is a third party; its XML is untrusted input like any other."""
    s = make_settings(tmp_path, plex_owner_token="ot")
    with pytest.raises(UpstreamError) as ei:
        await plex_tv.list_server_sections(_owner_http(_ENTITY_BOMB), s)
    assert "unsafe" in str(ei.value)


async def test_external_entity_reference_is_refused(tmp_path):
    s = make_settings(tmp_path, plex_owner_token="ot")
    with pytest.raises(UpstreamError) as ei:
        await plex_tv.list_server_sections(_owner_http(_EXTERNAL_ENTITY), s)
    assert "unsafe" in str(ei.value)


async def test_a_well_formed_non_mediacontainer_document_is_refused(tmp_path):
    """`<html>502</html>` parses fine and contains no Sections.

    On the share/invite readers that silence would mean "nobody has access",
    so the root tag is checked rather than the element count.
    """
    s = make_settings(tmp_path, plex_owner_token="ot")
    with pytest.raises(UpstreamError) as ei:
        await plex_tv.list_server_sections(_owner_http("<html>502 Bad Gateway</html>"), s)
    assert "MediaContainer" in str(ei.value)


def test_product_name_follows_app_name_everywhere_plex_sees_it(tmp_path):
    """One setting, both surfaces.

    This is the name that shows up in every user's Plex *Authorized Devices*
    list, and it reaches plex.tv twice: as a header on the API calls and as a
    query parameter on the hosted auth page. A rebrand that fixed only one
    would leave two differently named entries there.
    """
    s = make_settings(tmp_path, app_name="Quorarr")
    assert plex_tv.plex_headers(s)["X-Plex-Product"] == "Quorarr"
    assert "%5Bproduct%5D=Quorarr" in plex_tv.auth_url(s, "abcd")


def test_app_name_with_a_space_is_url_encoded_in_the_auth_url(tmp_path):
    """APP_NAME is operator-supplied and lands in a query string."""
    s = make_settings(tmp_path, app_name="Our Films")
    assert "%5Bproduct%5D=Our%20Films&" in plex_tv.auth_url(s, "abcd")
