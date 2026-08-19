"""Jellyseerr API client: request list, discover/search browse, and filing requests.

Every call goes through ``CachedHTTP`` with ``service="jellyseerr"``. The
``list_requests`` function retrieves pending and completed media requests
with configurable TTL-based caching.

The v0.4.0 additions (search, discover shelves, media detail, user lookup and
``create_request``) are the read+write surface behind the Discover tab.
Two house rules are enforced here rather than at the route, because this is
the only place that talks to Jellyseerr:

* **No 4K, anywhere.** ``create_request`` never sends an ``is4k`` field, and
  every status read here is the standard-quality one (``status``, never
  ``status4k``). 4K stays the owner's manual valve.
* **People are not media.** ``GET /search`` mixes ``mediaType: "person"`` rows
  into its results; they carry no tmdbId anyone can request, so ``shape_media``
  drops them instead of letting them reach a grid as unclickable cards. Since
  1.2.0 they are not thrown away, only kept apart: ``shape_person`` gives them
  their own shape, and the search dropdown renders that shape as a route to a
  filmography rather than as something to request.
"""

from typing import Any
from urllib.parse import quote

from pensieve.clients.base import CachedHTTP
from pensieve.config import Settings

# Jellyseerr media request status codes mapped to human-readable labels.
STATUS_LABELS = {
    1: "requested",
    2: "requested",
    3: "processing",
    4: "partially_available",
    5: "available",
}

#: Media status -> the badge vocabulary the Discover UI speaks. Anything not
#: listed (None = Jellyseerr has never heard of it, 1 = UNKNOWN, and the 2.x
#: statuses past 5 for deleted/blacklisted media) is treated as free to ask
#: for: Jellyseerr itself is the authority on duplicates and answers 409 if
#: it disagrees, which is a far better failure than hiding a Request button
#: from someone who is entitled to press it.
_AVAILABILITY = {2: "requested", 3: "requested", 4: "partial", 5: "available"}


def _base(settings: Settings) -> str:
    """Build the Jellyseerr v1 API base URL from settings."""
    return f"{settings.jellyseerr_url}/api/v1"


def _headers(settings: Settings) -> dict[str, str]:
    """Build the standard headers Jellyseerr requires on every request."""
    return {"X-Api-Key": settings.jellyseerr_api_key}


def shape_requests(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Shape a raw Jellyseerr request-list body into the frontend's request dicts.

    Split out from ``list_requests`` so a stale (cached-but-unshaped) response
    fetched via ``CachedHTTP.stale`` can be shaped the same way a live one is.

    Args:
        raw: Raw Jellyseerr response body, as returned by ``GET /api/v1/request``.

    Returns:
        One dict per request: ``id``, ``media_type``, ``tmdb_id``, ``tvdb_id``,
        ``status``, ``requested_by``, ``created_at``. ``tvdb_id`` is ``None`` for
        movies; ``requested_by`` falls back to ``plexUsername`` if ``displayName``
        is absent.
    """
    return [
        {
            "id": req["id"],
            "media_type": (req.get("media") or {}).get("mediaType"),
            "tmdb_id": (req.get("media") or {}).get("tmdbId"),
            "tvdb_id": (req.get("media") or {}).get("tvdbId"),
            "status": (req.get("media") or {}).get("status"),
            "requested_by": (req.get("requestedBy") or {}).get("displayName") or (req.get("requestedBy") or {}).get("plexUsername"),
            "created_at": req.get("createdAt"),
        }
        for req in raw["results"]
    ]


async def list_requests(http: CachedHTTP, settings: Settings, ttl: float = 30) -> list[dict[str, Any]]:
    """List all media requests from Jellyseerr, shaped for the frontend.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``jellyseerr_url``/``jellyseerr_api_key``.
        ttl: Cache freshness window in seconds.

    Returns:
        See ``shape_requests``.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    body = await http.get_json(
        f"{_base(settings)}/request",
        service="jellyseerr",
        headers=_headers(settings),
        params={"take": 50, "sort": "added", "filter": "all"},
        ttl=ttl,
    )
    return shape_requests(body)


def availability_of(status: int | None) -> str:
    """Map a Jellyseerr media status int to the UI's badge vocabulary.

    Args:
        status: ``mediaInfo.status``, or None when Jellyseerr has no record
            of the title at all.

    Returns:
        One of ``"available"``, ``"partial"``, ``"requested"``,
        ``"requestable"``.
    """
    return _AVAILABILITY.get(status, "requestable")


def _year(value: Any) -> int | None:
    """The year out of a Jellyseerr ``YYYY-MM-DD`` date, or None if unusable.

    Unreleased and metadata-poor titles routinely carry ``""`` or no date at
    all, which must read as "no year" rather than as a parse failure.
    """
    if not isinstance(value, str) or len(value) < 4 or not value[:4].isdigit():
        return None
    return int(value[:4])


def shape_media(item: dict[str, Any]) -> dict[str, Any] | None:
    """Shape one search/discover result into the frontend's card dict.

    Args:
        item: A raw row from ``/search`` or any ``/discover/*`` endpoint.

    Returns:
        ``tmdb_id``, ``title``, ``year``, ``media_type``, ``poster_path``,
        ``overview``, ``rating``, ``status`` and the derived ``availability``.
        ``poster_path`` stays the TMDB-relative path Jellyseerr returns; the
        frontend prefixes the CDN size it wants. Returns None for anything
        that is not a requestable title -- a person row, or a row with no
        tmdbId to request.
    """
    media_type = item.get("mediaType")
    tmdb_id = item.get("id")
    if media_type not in ("movie", "tv") or not isinstance(tmdb_id, int):
        return None

    info = item.get("mediaInfo") or {}
    status = info.get("status")
    return {
        "tmdb_id": tmdb_id,
        "title": item.get("title") or item.get("name") or "",
        "year": _year(item.get("releaseDate") or item.get("firstAirDate")),
        "media_type": media_type,
        "poster_path": item.get("posterPath"),
        "overview": item.get("overview") or "",
        "rating": item.get("voteAverage"),
        "status": status,
        "availability": availability_of(status),
    }


def shape_results(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Shape a paged ``{"results": [...]}`` body, dropping non-media rows.

    Args:
        raw: Raw body from ``/search`` or a ``/discover/*`` endpoint.

    Returns:
        Shaped cards in upstream order (Jellyseerr's own relevance/popularity
        ordering is the one worth keeping).
    """
    shaped = (shape_media(item) for item in raw.get("results") or [])
    return [row for row in shaped if row is not None]


def shape_person(item: dict[str, Any]) -> dict[str, Any] | None:
    """Shape one ``mediaType: "person"`` row into a suggestion row.

    Deliberately shares no field names with ``shape_media`` beyond
    ``media_type``: a person has a ``person_id``, not a ``tmdb_id``, because
    the two ids address different endpoints and a row that carried the wrong
    one would fail as a confusing 404 rather than as a type error. The
    frontend discriminates on ``media_type == "person"``.

    Args:
        item: A raw row from ``/search``.

    Returns:
        ``{"person_id", "name", "profile_path", "media_type"}``, or None for
        anything that is not a usable person -- a title row, or a person with
        no id or no name to render.
    """
    if item.get("mediaType") != "person":
        return None

    person_id = item.get("id")
    name = item.get("name")
    if not isinstance(person_id, int) or not name:
        return None

    return {
        "person_id": person_id,
        "name": name,
        # TMDB-relative, exactly like ``poster_path``; the frontend picks a size.
        "profile_path": item.get("profilePath"),
        "media_type": "person",
    }


def shape_suggestions(raw: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    """Shape a ``/search`` body into a short mixed list of titles and people.

    Upstream relevance order is preserved rather than grouped by kind. Sorting
    people to the top would answer *matrix* with the actor "Aurora Matrix"
    above the film, which is exactly backwards; upstream already ranks the
    person first for *tom hanks*, which is exactly right.

    Args:
        raw: Raw body from ``/search``.
        limit: How many rows the dropdown gets. Counted after shaping, so an
            unusable row costs nothing rather than one of the slots.

    Returns:
        Up to ``limit`` rows, each either a ``shape_media`` card or a
        ``shape_person`` row.
    """
    rows: list[dict[str, Any]] = []
    for item in raw.get("results") or []:
        shaped = (
            shape_person(item)
            if item.get("mediaType") == "person"
            else shape_media(item)
        )
        if shaped is None:
            continue
        rows.append(shaped)
        if len(rows) >= limit:
            break
    return rows


async def _search_body(
    http: CachedHTTP, settings: Settings, query: str, ttl: float
) -> dict[str, Any]:
    """GET Jellyseerr's combined index, unshaped.

    Both ``search`` and ``suggest`` come through here so they build a
    byte-identical request and therefore share one ``CachedHTTP`` entry. The
    frontend fires both routes for the same keystroke -- the dropdown and the
    results grid -- and this is what keeps that one upstream call rather than
    two.

    The query is percent-encoded into the URL rather than passed as a
    ``params`` entry, and that is not a style choice. httpx *form*-encodes a
    params mapping, so a space leaves as ``+``; Jellyseerr rejects that with
    400 *"Parameter \'query\' must be url encoded. Its value may not contain
    reserved characters."*, which this app then reports as "jellyseerr
    unreachable". Every multi-word search failed that way until 1.2.0.
    ``safe=""`` also encodes ``&`` and ``=``, so a title containing one cannot
    open a second query parameter.

    ``page`` rides in the URL too: httpx *replaces* a URL's query string with
    the params mapping rather than merging into it, so passing one here would
    silently drop the query that was just encoded.
    """
    return await http.get_json(
        f"{_base(settings)}/search?page=1&query={quote(query, safe='')}",
        service="jellyseerr",
        headers=_headers(settings),
        ttl=ttl,
    )


async def _browse(
    http: CachedHTTP, settings: Settings, path: str, ttl: float, **params: Any
) -> list[dict[str, Any]]:
    """GET a paged Jellyseerr browse endpoint and shape it into cards."""
    body = await http.get_json(
        f"{_base(settings)}{path}",
        service="jellyseerr",
        headers=_headers(settings),
        params={"page": 1, **params},
        ttl=ttl,
    )
    return shape_results(body)


async def search(
    http: CachedHTTP, settings: Settings, query: str, ttl: float = 60
) -> list[dict[str, Any]]:
    """Search Jellyseerr's combined movie/TV/person index.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        query: The user's raw search string (bounded by the caller).
        ttl: Cache freshness window in seconds.

    Returns:
        Shaped cards, people already dropped. See ``shape_media``.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    return shape_results(await _search_body(http, settings, query, ttl))


async def suggest(
    http: CachedHTTP, settings: Settings, query: str, ttl: float = 60, limit: int = 8
) -> list[dict[str, Any]]:
    """The same search, shaped for the type-ahead dropdown.

    Differs from ``search`` in two ways and no others: people are kept (as
    ``shape_person`` rows), and the list is cut to ``limit``. Same upstream
    call, same cache entry -- see ``_search_body``.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        query: The user's raw search string (bounded by the caller).
        ttl: Cache freshness window in seconds.
        limit: How many rows to return.

    Returns:
        Up to ``limit`` mixed title/person rows. See ``shape_suggestions``.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    return shape_suggestions(await _search_body(http, settings, query, ttl), limit)


async def discover_trending(
    http: CachedHTTP, settings: Settings, ttl: float = 900
) -> list[dict[str, Any]]:
    """Trending titles across both movies and TV. See ``search`` for semantics."""
    return await _browse(http, settings, "/discover/trending", ttl)


async def discover_movies_popular(
    http: CachedHTTP, settings: Settings, ttl: float = 900
) -> list[dict[str, Any]]:
    """Popular films. See ``search`` for semantics."""
    return await _browse(http, settings, "/discover/movies", ttl)


async def discover_movies_upcoming(
    http: CachedHTTP, settings: Settings, ttl: float = 900
) -> list[dict[str, Any]]:
    """Films not out yet. See ``search`` for semantics."""
    return await _browse(http, settings, "/discover/movies/upcoming", ttl)


async def browse_page(
    http: CachedHTTP,
    settings: Settings,
    path: str,
    params: dict[str, Any],
    ttl: float = 300,
) -> dict[str, Any]:
    """GET one page of a discover endpoint, keeping the pagination envelope.

    ``_browse`` deliberately throws the envelope away -- the shelves only
    ever want page one. Infinite scroll needs to know whether another page
    exists, so this keeps ``page``/``totalPages`` and derives ``has_more``
    here rather than making the frontend compare two numbers.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        path: Endpoint path, e.g. ``/discover/movies``. Chosen by
            ``services.browse.to_upstream``, never by a caller's input.
        params: Query parameters, likewise from ``to_upstream``.
        ttl: Cache freshness window in seconds.

    Returns:
        ``{"items": [...], "page": int, "total_pages": int, "has_more": bool}``.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    body = await http.get_json(
        f"{_base(settings)}{path}",
        service="jellyseerr",
        headers=_headers(settings),
        params=params,
        ttl=ttl,
    )
    page = body.get("page") or 1
    total_pages = body.get("totalPages") or 1
    return {
        "items": shape_results(body),
        "page": page,
        "total_pages": total_pages,
        "has_more": page < total_pages,
    }


async def genres(
    http: CachedHTTP, settings: Settings, media_type: str, ttl: float = 86400
) -> list[dict[str, Any]]:
    """The TMDB genre vocabulary for one media type.

    Movie and TV vocabularies genuinely differ -- films have ``Action`` (28),
    shows have ``Action & Adventure`` (10759) -- so a genre id only means
    something alongside its media type. Cached for a day: this list changes
    on the order of never.

    Note this endpoint answers with a bare JSON *array*, not the usual
    ``{"results": [...]}`` envelope.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        media_type: ``"movie"`` or ``"tv"``.
        ttl: Cache freshness window in seconds.

    Returns:
        ``[{"id": int, "name": str}, ...]``, malformed rows dropped.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    body = await http.get_json(
        f"{_base(settings)}/genres/{media_type}",
        service="jellyseerr",
        headers=_headers(settings),
        ttl=ttl,
    )
    return [
        {"id": row["id"], "name": row["name"]}
        for row in body or []
        if isinstance(row, dict) and isinstance(row.get("id"), int) and row.get("name")
    ]


def _shape_seasons(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-season rows for a TV detail, each marked requestable or not.

    Two different season lists have to be joined: TMDB's (``seasons``, which
    knows every season and its episode count) and Jellyseerr's own
    (``mediaInfo.seasons``, which knows what is already on the server or
    already asked for, and only lists seasons it has heard of).

    Season 0 is dropped. Specials are not what anyone means by "get me this
    show", Sonarr does not monitor them by default, and pre-checking them
    would quietly request a few hundred episodes -- the GoT specials row
    alone claims 300.
    """
    known = {
        s.get("seasonNumber"): s.get("status")
        for s in (raw.get("mediaInfo") or {}).get("seasons") or []
    }
    seasons = []
    for season in raw.get("seasons") or []:
        number = season.get("seasonNumber")
        if not isinstance(number, int) or number == 0:
            continue
        availability = availability_of(known.get(number))
        seasons.append(
            {
                "season_number": number,
                "name": season.get("name") or f"Season {number}",
                "episode_count": season.get("episodeCount") or 0,
                "air_date": season.get("airDate"),
                "availability": availability,
                "requestable": availability == "requestable",
            }
        )
    return seasons


async def media_detail(
    http: CachedHTTP, settings: Settings, media_type: str, tmdb_id: int, ttl: float = 60
) -> dict[str, Any]:
    """One title in full, including per-season availability for TV.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        media_type: ``"movie"`` or ``"tv"``.
        tmdb_id: TMDB id, as carried on every card.
        ttl: Cache freshness window in seconds.

    Returns:
        A ``shape_media`` card plus ``runtime`` (movies) and ``seasons``
        (TV; None for a movie, so "no seasons" and "an empty season list"
        stay distinguishable).

    Raises:
        ValueError: If ``media_type`` is not a requestable media type.
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    if media_type not in ("movie", "tv"):
        raise ValueError(f"unsupported media type: {media_type!r}")

    raw = await http.get_json(
        f"{_base(settings)}/{media_type}/{tmdb_id}",
        service="jellyseerr",
        headers=_headers(settings),
        ttl=ttl,
    )
    # The detail body has no mediaType of its own -- the endpoint is the type.
    card = shape_media({**raw, "mediaType": media_type})
    if card is None:  # pragma: no cover -- upstream would have 404'd first
        raise ValueError(f"jellyseerr returned no usable {media_type} {tmdb_id}")

    card["runtime"] = raw.get("runtime")
    card["seasons"] = _shape_seasons(raw) if media_type == "tv" else None
    return card


#: How many acting credits a filmography shows. A prolific character actor has
#: several hundred, most of them a single scene in something nobody is looking
#: for; the top fifty by popularity is the part anyone scrolls.
_MAX_CREDITS = 50


async def person(
    http: CachedHTTP, settings: Settings, person_id: int, ttl: float = 600
) -> dict[str, Any]:
    """One person's name and headshot.

    A separate call from the credits because ``/combined_credits`` answers
    with ``{"cast", "crew", "id"}`` and no name at all -- there is nothing to
    title the filmography page with otherwise.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        person_id: TMDB person id, as carried on a suggestion row.
        ttl: Cache freshness window in seconds.

    Returns:
        A ``shape_person`` row.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response --
            including the 404 that means there is no such person.
    """
    raw = await http.get_json(
        f"{_base(settings)}/person/{person_id}",
        service="jellyseerr",
        headers=_headers(settings),
        ttl=ttl,
    )
    # The endpoint is the type; the body carries no mediaType of its own.
    shaped = shape_person({**raw, "mediaType": "person"})
    if shaped is None:  # pragma: no cover -- upstream would have 404'd first
        raise ValueError(f"jellyseerr returned no usable person {person_id}")
    return shaped


async def person_credits(
    http: CachedHTTP, settings: Settings, person_id: int, ttl: float = 600
) -> list[dict[str, Any]]:
    """One person's *acting* credits, shaped as ordinary Discover cards.

    Only ``cast`` is read. ``crew`` is a different question ("what did they
    direct") and mixing the two produces a list where a producer credit on a
    film they are not in sits next to their starring roles.

    Three things happen to that array, in order:

    * **Sorted by vote count**, which upstream does not do -- it returns
      credits in TMDB's own order, which is neither chronological nor useful.
      Sorting before the cut is what makes the cut keep the top rather than an
      arbitrary slice.

      Vote count rather than ``popularity``, which is what 1.2.0 shipped and
      got wrong. TMDB's ``popularity`` is a rolling *trending* score, so a
      daily talk show someone guested on once buries everything they are known
      for: it left only ten of Tom Hanks's fifty rows as films, topped by The
      Simpsons and The Daily Show, with Forrest Gump far down the grid.
      ``voteCount`` is how many people cared enough to rate the title, which
      does not decay -- the same fifty become 48 films led by Forrest Gump,
      Toy Story and The Green Mile.
    * **De-duplicated**, on ``(media_type, tmdb_id)`` rather than the bare id.
      TMDB emits one row per *credit*, so a second role in the same film is a
      second row; and its id space is per media type, so film 13 and show 13
      are two different titles that a bare-id key would silently merge.
    * **Capped** at ``_MAX_CREDITS``.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        person_id: TMDB person id.
        ttl: Cache freshness window in seconds.

    Returns:
        Shaped cards, identical in shape to a search result -- which is what
        lets the filmography reuse the poster tile and the request sheet
        without a second code path. Non-title rows are dropped.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    raw = await http.get_json(
        f"{_base(settings)}/person/{person_id}/combined_credits",
        service="jellyseerr",
        headers=_headers(settings),
        ttl=ttl,
    )

    ranked = sorted(
        raw.get("cast") or [],
        key=lambda credit: credit.get("voteCount") or 0,
        reverse=True,
    )

    seen: set[tuple[str, int]] = set()
    cards: list[dict[str, Any]] = []
    for credit in ranked:
        card = shape_media(credit)
        if card is None:
            continue
        key = (card["media_type"], card["tmdb_id"])
        if key in seen:
            continue
        seen.add(key)
        cards.append(card)
        if len(cards) >= _MAX_CREDITS:
            break
    return cards


async def list_users(
    http: CachedHTTP, settings: Settings, ttl: float = 300
) -> list[dict[str, Any]]:
    """Every Jellyseerr account, reduced to the pair that identifies it.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        ttl: Cache freshness window in seconds. Pass 0 to force a refetch --
            which is what the mapping helper does straight after importing a
            new user, since the cached list predates them.

    Returns:
        ``[{"id": <jellyseerr user id>, "plex_id": <plex account id>}, ...]``.
        Accounts with no ``plexId`` (local Jellyseerr logins) are skipped:
        nothing here can ever match them, since a session's identity
        *is* a Plex account id.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    body = await http.get_json(
        f"{_base(settings)}/user",
        service="jellyseerr",
        headers=_headers(settings),
        params={"take": 100},
        ttl=ttl,
    )
    return [
        {"id": user["id"], "plex_id": user["plexId"]}
        for user in body.get("results") or []
        if isinstance(user.get("plexId"), int) and isinstance(user.get("id"), int)
    ]


async def import_plex_users(
    http: CachedHTTP, settings: Settings, plex_ids: list[int]
) -> list[dict[str, Any]]:
    """Import Plex-shared accounts into Jellyseerr so they can own requests.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        plex_ids: Plex account ids to import. Jellyseerr wants them as
            strings, so they are stringified here rather than at the caller.

    Returns:
        The created user objects, as Jellyseerr reports them (possibly empty
        if it declined to import any).

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    return await http.send_json(
        "POST",
        f"{_base(settings)}/user/import-from-plex",
        service="jellyseerr",
        headers=_headers(settings),
        json={"plexIds": [str(plex_id) for plex_id in plex_ids]},
    )


async def create_request(
    http: CachedHTTP,
    settings: Settings,
    *,
    media_type: str,
    tmdb_id: int,
    user_id: int,
    seasons: list[int] | None = None,
    profile_id: int | None = None,
) -> Any:
    """File a media request, attributed to a specific Jellyseerr user.

    ``userId`` is the whole point of this call: without it Jellyseerr credits
    the request to the API key's owner, which would make every
    friend's request look like theirs and spend the wrong person's quota.

    No ``is4k`` field is sent, by house policy -- see the module docstring.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        media_type: ``"movie"`` or ``"tv"``.
        tmdb_id: TMDB id of the title.
        user_id: Jellyseerr user id to attribute the request to.
        seasons: Season numbers, TV only.
        profile_id: Arr quality profile to file the request against, sent as
            ``profileId``. Omitted when None, which is the only way to get
            Jellyseerr's own default -- the request route always
            passes one (see ``services.discover.profile_for``). Note this is
            the *standard* profile field; ``is4k`` is never sent, so a 4K
            profile id here still files a standard-lane request against the
            4K-quality profile rather than flipping Jellyseerr's 4K switch.

    Returns:
        Jellyseerr's created-request object.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
            A 4xx carries Jellyseerr's own wording on ``.detail`` (e.g. the
            duplicate-request refusal), which the route surfaces verbatim.
    """
    body: dict[str, Any] = {"mediaType": media_type, "mediaId": tmdb_id, "userId": user_id}
    if seasons is not None:
        body["seasons"] = seasons
    if profile_id is not None:
        body["profileId"] = profile_id

    return await http.send_json(
        "POST",
        f"{_base(settings)}/request",
        service="jellyseerr",
        headers=_headers(settings),
        json=body,
    )
