"""plex.tv client: OAuth PIN flow + server-access authorization check.

Implements the plex.tv "linkedin" PIN auth handshake used to sign a user in
without ever seeing their password, plus the follow-up check that the signed
in account actually has a share on OUR Plex server (not just any valid Plex
account). Every call goes through ``CachedHTTP`` with ``ttl=0`` — auth
responses must never be served stale from cache.
"""

from typing import Any
from urllib.parse import quote

# Parsing goes through defusedxml; the stdlib module is imported only for the
# Element type and ParseError. plex.tv is a third party talking to us over the
# network, so its XML is untrusted input like any other: stdlib ElementTree
# does not resolve external entities, but it will happily expand an internal
# entity bomb (the "billion laughs" quadratic-blowup DoS) inside a process
# that is also serving every friend's dashboard.
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as parse_xml

from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.config import Settings

PLEX_TV_BASE = "https://plex.tv/api/v2"
PLEX_TV_SERVERS = "https://plex.tv/api/servers"
#: Share invites the owner has sent that nobody has accepted yet. Despite the
#: name this is the *outgoing* pending list for a server owner (verified live
#: 2026-08-14); ``/api/invites/sent`` is a 404 on this account.
PLEX_TV_INVITES_REQUESTED = "https://plex.tv/api/invites/requested"
PLEX_APP_AUTH_URL = "https://app.plex.tv/auth#?"


def plex_headers(settings: Settings, token: str | None = None) -> dict[str, str]:
    """Build the standard headers plex.tv requires on every request.

    Args:
        settings: App settings, for ``plex_client_id`` and ``app_name``.
        token: Optional Plex auth token to include as ``X-Plex-Token``.

    Returns:
        Header dict for use with httpx.
    """
    headers = {
        "X-Plex-Product": settings.app_name,
        "X-Plex-Client-Identifier": settings.plex_client_id,
        "Accept": "application/json",
    }
    if token is not None:
        headers["X-Plex-Token"] = token
    return headers


async def create_pin(http: CachedHTTP, settings: Settings) -> dict[str, Any]:
    """Request a new plex.tv auth PIN.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for client headers.

    Returns:
        ``{"id": int, "code": str}``.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    body = await http.send_json(
        "POST",
        f"{PLEX_TV_BASE}/pins",
        service="plex.tv",
        headers=plex_headers(settings),
        params={"strong": "true"},
    )
    return {"id": body["id"], "code": body["code"]}


def auth_url(settings: Settings, code: str) -> str:
    """Build the plex.tv hosted auth page URL for a PIN code.

    Args:
        settings: App settings, for ``plex_client_id``, ``app_name``, and
            ``base_url``.
        code: The PIN code from ``create_pin``.

    Returns:
        Full URL to send the user to for authentication.
    """
    forward_url = quote(f"{settings.base_url}/auth/callback", safe="")
    # The product name reaches plex.tv twice -- as a header on the API calls
    # and as this query parameter on the hosted page -- and both land in the
    # user's Authorized Devices list. Both read the same setting; a rebrand
    # that fixed only one would leave a split identity there.
    product = quote(settings.app_name, safe="")
    return (
        f"{PLEX_APP_AUTH_URL}"
        f"clientID={settings.plex_client_id}"
        f"&code={code}"
        f"&context%5Bdevice%5D%5Bproduct%5D={product}"
        f"&forwardUrl={forward_url}"
    )


async def fetch_pin(http: CachedHTTP, settings: Settings, pin_id: int) -> dict[str, Any]:
    """Read a PIN's current state from plex.tv.

    plex.tv scopes this by client identifier: polling a PIN minted under a
    *different* ``X-Plex-Client-Identifier`` answers 404 (verified live,
    2026-08-13). That is the whole reason a PIN id is not a secret between
    apps -- but it is no protection *within* one app, where every browser
    shares this app's client id. Hence ``code``, which the caller checks:
    see ``api/auth_routes.callback``.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for client headers.
        pin_id: The PIN id from ``create_pin`` (or from the browser).

    Returns:
        The PIN object: ``{"id", "code", "authToken", ...}``. ``authToken``
        is None until someone signs in against it.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response
            (including the 404 for an unknown or wrong-client PIN).
    """
    return await http.get_json(
        f"{PLEX_TV_BASE}/pins/{pin_id}",
        service="plex.tv",
        headers=plex_headers(settings),
        ttl=0,
    )


async def poll_pin(http: CachedHTTP, settings: Settings, pin_id: int) -> str | None:
    """Check whether a PIN has been claimed by a signed-in user.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for client headers.
        pin_id: The PIN id from ``create_pin``.

    Returns:
        The auth token if the PIN has been claimed, else None.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    return (await fetch_pin(http, settings, pin_id)).get("authToken")


async def get_user(http: CachedHTTP, token: str, settings: Settings) -> dict[str, Any]:
    """Fetch the plex.tv account for an auth token.

    Args:
        http: Shared cached HTTP client.
        token: The user's Plex auth token.
        settings: App settings, for client headers.

    Returns:
        ``{"id": int, "name": str, "thumb": str, "email": str}``, where
        ``name`` is the account's ``friendlyName`` falling back to
        ``username``. ``email`` is what the plex.tv share API invites, and is
        "" for the (rare) account that exposes none -- callers must handle
        that rather than assume an address is always available.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    body = await http.get_json(
        f"{PLEX_TV_BASE}/user",
        service="plex.tv",
        headers=plex_headers(settings, token),
        ttl=0,
    )
    return {
        "id": body["id"],
        "name": body.get("friendlyName") or body["username"],
        "thumb": body["thumb"],
        "email": body.get("email") or "",
    }


async def has_server_access(http: CachedHTTP, token: str, settings: Settings) -> bool:
    """Check whether the token's account has a share on OUR Plex server.

    This is the authorization gate for login: a valid Plex account that has
    no share on our server (``settings.plex_server_machine_id``) must not be
    treated as authenticated.

    Args:
        http: Shared cached HTTP client.
        token: The user's Plex auth token.
        settings: App settings, for ``plex_server_machine_id``.

    Returns:
        True iff one of the account's resources matches our server's machine id.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    if not settings.plex_server_machine_id:
        # Fail closed: a blank configured machine id must never match a
        # resource with an equally blank/missing clientIdentifier.
        return False

    resources = await http.get_json(
        f"{PLEX_TV_BASE}/resources",
        service="plex.tv",
        headers=plex_headers(settings, token),
        ttl=0,
    )
    return any(
        resource.get("clientIdentifier") == settings.plex_server_machine_id
        for resource in resources
    )


def _owner_headers(settings: Settings) -> dict[str, str]:
    """Headers for an owner-authenticated plex.tv call.

    Raises:
        UpstreamError: If ``plex_owner_token`` is unset. Failing here (rather
            than sending an unauthenticated request and reading plex.tv's
            401 back) keeps a misconfiguration from looking like an outage.
    """
    if not settings.plex_owner_token:
        raise UpstreamError("plex.tv", "plex.tv owner token not configured")
    return plex_headers(settings, settings.plex_owner_token)


async def list_server_sections(http: CachedHTTP, settings: Settings) -> list[int]:
    """List the library section ids on OUR Plex server.

    Sharing is per-section, so an invite needs the id of every library the
    friend should see. This is a plex.tv **v1** endpoint and answers with XML
    no matter what ``Accept`` says, hence ``_owner_xml`` rather than
    ``get_json``.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``plex_server_machine_id`` and
            ``plex_owner_token``.

    Returns:
        Every ``<Section>`` id on the server, ascending. (Order was once
        "as they appear in the document"; these are a set of ids posted as
        ``library_section_ids``, so nothing has ever read meaning into it.)

    Raises:
        UpstreamError: See ``_owner_xml``.
    """
    root = await _owner_xml(
        http, settings, f"{PLEX_TV_SERVERS}/{settings.plex_server_machine_id}"
    )
    # sorted(), not the set itself: this list is JSON-serialised into the
    # share POST, and json cannot encode a set.
    return sorted(_int_attrs(root, "Section", "id"))


async def _owner_xml(http: CachedHTTP, settings: Settings, url: str) -> Element:
    """GET an owner-authenticated plex.tv v1 endpoint and parse its XML.

    The single door every plex.tv v1 (XML) response comes through, so the
    hardening below is written once rather than per caller.

    Raises:
        UpstreamError: On a missing owner token, a connect/transport error, a
            non-2xx response, a body that isn't parseable XML, a body that
            declares entities or external references, or a well-formed
            document that simply isn't a MediaContainer. Callers use these
            lists to decide who to *revoke*, so "I couldn't read it" must
            never be able to arrive as an empty set — that would read as
            "nobody has a share" and cut off the entire membership.
    """
    body = await http.get_text(url, service="plex.tv", headers=_owner_headers(settings))
    try:
        root = parse_xml(body)
    except ParseError as exc:
        raise UpstreamError("plex.tv", f"plex.tv returned unparseable XML: {exc}") from exc
    except DefusedXmlException as exc:
        # Entity/DTD/external-reference bombs. Distinct from a parse failure
        # on purpose: this is a well-formed document doing something a
        # legitimate plex.tv response never does, and it is worth being able
        # to tell those apart in the logs.
        raise UpstreamError("plex.tv", f"plex.tv XML rejected as unsafe: {exc}") from exc

    # "Parsed" is not "understood". An HTML error page from a proxy —
    # `<html>gateway error</html>` — is perfectly well-formed XML, and would
    # otherwise sail through here to yield an empty set of shares, i.e. "no
    # one has access", i.e. revoke the entire membership. plex.tv answers
    # every v1 endpoint with a MediaContainer root; anything else is someone
    # else talking.
    if root.tag != "MediaContainer":
        raise UpstreamError(
            "plex.tv", f"plex.tv returned a <{root.tag}> document, not a MediaContainer"
        )
    return root


def _int_attrs(root: Element, tag: str, attr: str) -> set[int]:
    """Every parseable integer ``attr`` on every ``tag`` under ``root``."""
    found = set()
    for element in root.iter(tag):
        raw = element.attrib.get(attr)
        if raw is None:
            continue
        try:
            found.add(int(raw))
        except ValueError:
            continue
    return found


async def list_shared_account_ids(http: CachedHTTP, settings: Settings) -> set[int]:
    """Plex account ids that have an **accepted** share on our server.

    Note the attribute: a ``<SharedServer>``'s own ``id`` is the id of the
    share record, not of the account — ``userID`` is the account. Reading the
    wrong one yields a set that matches nothing and revokes everybody.

    Raises:
        UpstreamError: See ``_owner_xml``.
    """
    root = await _owner_xml(
        http, settings, f"{PLEX_TV_SERVERS}/{settings.plex_server_machine_id}/shared_servers"
    )
    return _int_attrs(root, "SharedServer", "userID")


async def list_pending_invites(http: CachedHTTP, settings: Settings) -> list[dict[str, Any]]:
    """Share invites sent but not yet accepted, with the detail an owner needs.

    An ``<Invite>``'s own ``id`` *is* the plex account id (verified live
    2026-08-14: the pending invite for account 835763331 carries
    ``id="835763331"``) — unlike ``<SharedServer>``, whose ``id`` is the
    share record. ``createdAt`` is a unix epoch, which is what lets the owner
    view say how long someone has been sitting unaccepted without a second
    call.

    Rows without a parseable id are dropped: they cannot be matched to a
    member, so they are noise rather than information.

    Raises:
        UpstreamError: See ``_owner_xml``.
    """
    root = await _owner_xml(http, settings, PLEX_TV_INVITES_REQUESTED)

    invites: list[dict[str, Any]] = []
    for element in root.iter("Invite"):
        raw = element.attrib.get("id")
        try:
            account_id = int(raw) if raw is not None else None
        except ValueError:
            account_id = None
        if account_id is None:
            continue

        try:
            invited_at = int(element.attrib.get("createdAt") or 0)
        except ValueError:
            invited_at = 0

        invites.append({
            "id": account_id,
            "username": element.attrib.get("username") or "",
            "email": element.attrib.get("email") or "",
            "invited_at": invited_at,
        })
    return invites


async def list_pending_invite_account_ids(http: CachedHTTP, settings: Settings) -> set[int]:
    """Plex account ids with a share invite sent but not yet accepted.

    An invited account appears *only* here — never in ``shared_servers``
    until it accepts — so anything reconciling membership has to union the
    two or it will revoke people in the gap between an owner approving them
    and them opening the Plex email.

    Raises:
        UpstreamError: See ``_owner_xml``.
    """
    return {invite["id"] for invite in await list_pending_invites(http, settings)}


async def invite_to_server(http: CachedHTTP, settings: Settings, *, email: str) -> None:
    """Share every library on our server with a Plex account, by email.

    This is the same wire format plexapi's ``inviteFriend`` sends: a POST to
    ``/api/servers/{machine_id}/shared_servers`` carrying the full section id
    list. The invited account gets a pending Plex invitation, which is what
    turns "denied at the door" into "signed in" once they accept it.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``plex_server_machine_id`` and
            ``plex_owner_token``.
        email: The invitee's plex.tv account email.

    Raises:
        UpstreamError: On a missing owner token, or any failure fetching the
            section list or posting the share.
    """
    headers = _owner_headers(settings)
    sections = await list_server_sections(http, settings)

    try:
        await http.send_json(
            "POST",
            f"{PLEX_TV_SERVERS}/{settings.plex_server_machine_id}/shared_servers",
            service="plex.tv",
            headers=headers,
            json={
                "server_id": settings.plex_server_machine_id,
                "shared_server": {
                    "library_section_ids": sections,
                    "invited_email": email,
                },
            },
        )
    except ValueError:
        # `send_json` parses the response body, and this v1 endpoint answers
        # with XML on success. The status check already passed by the time
        # parsing runs, so a decode failure here means the share landed --
        # UpstreamError (a real failure) is not a ValueError and still raises.
        return
