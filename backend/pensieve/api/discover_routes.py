"""Discover: browse, search, and file a Jellyseerr request from inside the app.

Session-gated like every other member surface. Three reads and one write:

* ``GET /api/discover/shelves`` — Trending / Popular / Coming soon, cached 15
  minutes because these lists move on a scale of days, not seconds.
* ``GET /api/discover/search?q=`` — Jellyseerr's combined index, cached 60s and
  bounded to 100 characters. The frontend debounces, but the bound is here:
  the query is a client-controlled string that becomes an upstream call.
* ``GET /api/discover/suggest?q=`` — the same index and the same cache entry,
  shaped for the type-ahead dropdown: people kept, list cut to eight.
* ``GET /api/discover/person/{person_id}`` — one actor's filmography, acting
  credits only, as ordinary Discover cards.
* ``GET /api/discover/detail/{media_type}/{tmdb_id}`` — one title, plus the
  per-season availability the TV picker needs.
* ``POST /api/discover/request`` — the only write, and the only place in
  the app that creates work for the download stack on a friend's say-so.

Three things guard that write, in order: a per-account hourly cap, fail-loud
user attribution (see ``services/discover.jellyseerr_user_id``), and a
re-check of the season pick against live availability — the picker disables
unavailable seasons client-side, which is a courtesy, not a control.

Quality is a server-side decision made from a *vocabulary*, never from a
client-supplied profile id: the body carries ``quality`` ("1080p", "720p",
"4K") and the mapping to an audited arr profile happens in
``services.discover.profile_for``. Every request files with an explicit
``profileId``, movies included -- inheriting Jellyseerr's default would be an
unaudited lane.

4K is the one asymmetric branch. The owner's 4K request files immediately;
anyone else's becomes a ``discover_4k_requests`` row awaiting approval, and
nothing reaches Jellyseerr until it is granted. The role is read from the session
dependency, never from the body, so "am I allowed 4K" is not a client-side
question. ``is4k`` itself is still never sent and still a 422 (the body model
forbids unknown fields) -- Jellyseerr's 4K switch stays the owner's manual valve;
the owner lane merely files against the 4K *profile*.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pensieve import notify
from pensieve.auth import current_user
from pensieve.clients import jellyseerr
from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.config import Settings
from pensieve.db import get_db
from pensieve.ratelimit import request_limiter
from pensieve.services import access, browse, discover

router = APIRouter(prefix="/api/discover", dependencies=[Depends(current_user)])

_SHELVES_TTL = 900
_SEARCH_TTL = 60
_DETAIL_TTL = 60
#: Browse pages move slower than search but faster than the shelves.
_BROWSE_TTL = 300
#: Genre vocabularies are effectively static.
_GENRES_TTL = 86400
#: A filmography changes when someone shoots a film. The availability badges on
#: it are the only part that moves, and ten minutes is well inside how long a
#: download takes to become watchable.
_PERSON_TTL = 600

#: How many rows the type-ahead dropdown gets. A bound on the *route* rather
#: than on the component: it is what the response promises, so a stale client
#: cannot ask for a thousand.
_SUGGEST_TAKE = 8

_INSERT_EVENT = "INSERT INTO events (at, actor, action, detail) VALUES (?, ?, ?, ?)"


def _unmapped(settings: Settings) -> str:
    """What a friend is told when their Plex account has no Jellyseerr user.

    Deliberately actionable and deliberately vague about the internals: the
    alternative (filing the request against whoever owns the API key) is
    worse than any error message.
    """
    return f"couldn't map your account — ask {settings.owner_name}"


#: Said to an approved member whose Plex invite is still unaccepted. The old
#: wording sent them to ask an owner who had already approved them.
_UNACCEPTED_INVITE = (
    "Accept the Plex invite in your email — that's what links your account "
    "to the server. Then try again."
)

#: Said when 720p is asked for on a deploy with no SONARR_PROFILE_720_ID.
#: A misconfigured lane must not silently become 1080p: the friend picked
#: the space-saver on purpose, and quietly filing the premium tier would
#: spend disk nobody agreed to.
_NO_720_LANE = "720p lane not configured"

_INSERT_4K_REQUEST = """
INSERT INTO discover_4k_requests
    (media_type, tmdb_id, title, seasons_json, requested_by, requested_by_name,
     state, created_at)
VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
"""

_PENDING_4K_DUPLICATE = """
SELECT id FROM discover_4k_requests
WHERE media_type = ? AND tmdb_id = ? AND state = 'pending'
"""


def _already_pending(settings: Settings) -> str:
    """Answer for both duplicate-detection layers.

    Said by the pre-check SELECT and by the ``idx_discover_4k_pending``
    unique index that closes its race.
    """
    return f"Already waiting on {settings.owner_name}'s sign-off."


#: Upstream titles are stored on the 4K row and later read straight into a
#: Web Push payload, which is size-capped. Jellyseerr has never sent anything
#: near this, but the string is not ours to trust unbounded.
_MAX_TITLE = 200

#: Jellyseerr 4xx statuses that are safe to relay to the friend as-is.
#: Deliberately a short allow-list rather than "any 4xx": those three are the
#: ones that mean something the *friend* did (bad title, no such title,
#: already requested). Everything else in the 4xx range is this app's problem
#: with Jellyseerr -- a 401 from a rotated API key, a 403, a 429 from
#: upstream's own limiter -- and relaying those statuses verbatim would reach
#: the browser as, e.g., a 401, which ``api.ts`` treats as "session gone" and
#: bounces the friend back to the login screen for a fault that was never
#: theirs. Those become the house 502 instead.
_RELAYABLE_STATUSES = frozenset({400, 404, 409})


def _unreachable(exc: UpstreamError) -> JSONResponse:
    """The house 502: service name only, never the upstream's exception text."""
    return JSONResponse({"error": f"{exc.service} unreachable"}, status_code=502)


@router.get("/shelves", response_model=None)
async def get_shelves(request: Request) -> dict[str, Any] | JSONResponse:
    """The three browse shelves.

    Returns:
        200 ``{"shelves": [{"id", "title", "items", "error"}, ...]}``. A single
        failed shelf comes back empty with its own ``error`` set. 502
        ``{"error": "jellyseerr unreachable"}`` only when no shelf loaded.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        return {"shelves": await discover.shelves(http, settings, ttl=_SHELVES_TTL)}
    except UpstreamError as exc:
        return _unreachable(exc)


@router.get("/search", response_model=None)
async def get_search(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=100)],
) -> dict[str, Any] | JSONResponse:
    """Search for something to request.

    Returns:
        200 ``{"items": [...]}`` (people already dropped). 422 if ``q`` is
        empty or over 100 characters. 502 ``{"error": "jellyseerr
        unreachable"}``.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        return {"items": await jellyseerr.search(http, settings, q, ttl=_SEARCH_TTL)}
    except UpstreamError as exc:
        return _unreachable(exc)


@router.get("/suggest", response_model=None)
async def get_suggest(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=100)],
) -> dict[str, Any] | JSONResponse:
    """Type-ahead matches for the Discover search field.

    Separate from ``/search`` rather than folded into it, because the two
    answer different questions: the grid is a list of things to request, and a
    person is not one of those. Keeping them apart means the grid's contract
    is untouched and no client has to filter a person out of a list of cards.
    Both read the same upstream body from the same cache entry, so the second
    of the two calls a keystroke fires costs nothing upstream.

    Returns:
        200 ``{"items": [...]}`` — up to eight rows, each either a title card
        or ``{"person_id", "name", "profile_path", "media_type": "person"}``,
        in upstream relevance order. 422 if ``q`` is empty or over 100
        characters. 502 ``{"error": "jellyseerr unreachable"}``.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        return {
            "items": await jellyseerr.suggest(
                http, settings, q, ttl=_SEARCH_TTL, limit=_SUGGEST_TAKE
            )
        }
    except UpstreamError as exc:
        return _unreachable(exc)


@router.get("/person/{person_id}", response_model=None)
async def get_person(
    person_id: Annotated[int, Path(ge=1)],
    request: Request,
) -> dict[str, Any] | JSONResponse:
    """One actor and what they have acted in.

    Acting credits only, most popular first, capped at fifty, and shaped like
    every other Discover card — so each one opens the same detail sheet and
    files through the same guarded request lane. See
    ``clients.jellyseerr.person_credits``.

    Returns:
        200 ``{"person_id", "name", "profile_path", "items": [...]}``. 404
        ``{"error": "not found"}`` if Jellyseerr has no such person — distinct
        from a 502 for the same reason the title detail route makes that
        distinction. 422 on a non-positive or non-numeric id. 502 ``{"error":
        "jellyseerr unreachable"}``.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        return await discover.filmography(http, settings, person_id, ttl=_PERSON_TTL)
    except UpstreamError as exc:
        if exc.status == 404:
            return JSONResponse({"error": "not found"}, status_code=404)
        return _unreachable(exc)


@router.get("/browse", response_model=None)
async def get_browse(
    request: Request,
    sort: browse.Sort = "popular",
    media: browse.Media | None = None,
    genre: int | None = None,
    decade: browse.Decade | None = None,
    min_rating: Annotated[int | None, Query(ge=7, le=8)] = None,
    page: Annotated[int, Query(ge=1, le=500)] = 1,
) -> dict[str, Any] | JSONResponse:
    """One page of filtered browse results.

    ``sort`` is an enum rather than a string precisely because Jellyseerr
    answers 200 for an unknown ``sortBy`` and quietly returns the default
    ordering -- a pass-through would turn a typo into an invisible wrong
    answer. Every filter defaults to None meaning *absent*: the mapper
    substitutes ``movie`` for non-trending sorts, and ``trending`` must be
    able to tell "no media supplied" from "movie supplied", or a bare
    trending request would trip its own 400.

    ``page`` is capped at 500, TMDB's own ceiling.

    Returns:
        200 ``{"items", "page", "total_pages", "has_more"}``. 400 if filters
        were combined with ``trending``, which accepts none. 422 on an
        unknown sort/decade/media, an out-of-range page, or a ``min_rating``
        outside 7-8 (a bounded ``int`` rather than a ``Literal[7, 8]`` --
        query parameters arrive as strings, and Pydantic v2 will not coerce
        a string into an int ``Literal`` member, so a literal type here
        rejects every value it is meant to accept). 502 ``{"error":
        "jellyseerr unreachable"}``.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        path, params = browse.to_upstream(
            sort=sort, media=media, genre=genre, decade=decade,
            min_rating=min_rating, page=page,
            today=datetime.now(timezone.utc).date(),
        )
    except browse.BrowseFilterConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        return await jellyseerr.browse_page(http, settings, path, params, ttl=_BROWSE_TTL)
    except UpstreamError as exc:
        return _unreachable(exc)


@router.get("/genres/{media_type}", response_model=None)
async def get_genres(
    media_type: Literal["movie", "tv"], request: Request
) -> dict[str, Any] | JSONResponse:
    """The genre vocabulary for one media type, for the browse filter bar.

    Returns:
        200 ``{"genres": [{"id", "name"}, ...]}``. 422 on an unknown media
        type. 502 ``{"error": "jellyseerr unreachable"}``.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        return {"genres": await jellyseerr.genres(http, settings, media_type, ttl=_GENRES_TTL)}
    except UpstreamError as exc:
        return _unreachable(exc)


@router.get("/detail/{media_type}/{tmdb_id}", response_model=None)
async def get_detail(
    media_type: Literal["movie", "tv"],
    tmdb_id: int,
    request: Request,
) -> dict[str, Any] | JSONResponse:
    """One title in full; TV carries per-season availability.

    Returns:
        200 with the detail card. 404 ``{"error": "not found"}`` if Jellyseerr
        has no such title -- distinct from a 502, because "no such film" and
        "the service is down" are different things to tell someone. 502
        ``{"error": "jellyseerr unreachable"}`` otherwise.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        return await jellyseerr.media_detail(
            http, settings, media_type, tmdb_id, ttl=_DETAIL_TTL
        )
    except UpstreamError as exc:
        if exc.status == 404:
            return JSONResponse({"error": "not found"}, status_code=404)
        return _unreachable(exc)


class RequestBody(BaseModel):
    """Request body for ``POST /api/discover/request``.

    ``extra="forbid"`` is load-bearing rather than tidy: it is what makes an
    ``is4k`` field a hard 422 instead of a silently dropped key, which is the
    difference between enforcing the policy and merely intending to. It is
    also why ``quality`` is a closed vocabulary rather than a profile id --
    the id is chosen here, from settings, and cannot be steered from outside.
    """

    model_config = ConfigDict(extra="forbid")

    media_type: Literal["movie", "tv"]
    tmdb_id: int = Field(ge=1)
    #: TV only. Bounded because it lands in an upstream body; season numbers
    #: run to double digits in the worst real case.
    seasons: list[Annotated[int, Field(ge=1, le=1000)]] | None = Field(
        default=None, max_length=100
    )
    #: Which audited lane to file against. Films have no quality choice
    #: (they pin to the Radarr HD lane), so "720p" on a movie is a 422
    #: rather than a silently ignored key -- a client that thinks it picked
    #: the space-saver for a film should be told it did not. "4K" is
    #: accepted from anyone here and gated by *role* in the route: a
    #: friend's 4K becomes an approval row, not a download.
    quality: Literal["1080p", "720p", "4K"] = "1080p"

    @model_validator(mode="after")
    def _720_is_tv_only(self) -> "RequestBody":
        """Refuse the space-saver lane on a film: Radarr has no 720p lane here."""
        if self.media_type == "movie" and self.quality == "720p":
            raise ValueError("720p is a TV-only lane")
        return self


async def _file_for_approval(
    http: CachedHTTP,
    db: sqlite3.Connection,
    settings: Settings,
    *,
    body: RequestBody,
    user: dict,
    title: str,
    seasons: list[int] | None,
    now: datetime,
) -> JSONResponse:
    """Park a friend's 4K request for the owner instead of filing it.

    Nothing upstream happens here by design: 4K is the expensive lane, and the
    point of the gate is that the owner decides *before* anything is
    downloaded, not that they clean up after. The seasons are stored verbatim
    so the approval files exactly the pick that was made, not whatever is
    still requestable by the time it gets looked at.

    Returns:
        202 ``{"ok": True, "state": "pending_approval", "id": int, "title":
        str}``, or 409 if the same title is already waiting.
    """
    if db.execute(_PENDING_4K_DUPLICATE, (body.media_type, body.tmdb_id)).fetchone():
        return JSONResponse({"error": _already_pending(settings)}, status_code=409)

    try:
        cur = db.execute(
            _INSERT_4K_REQUEST,
            (
                body.media_type,
                body.tmdb_id,
                title[:_MAX_TITLE],
                json.dumps(seasons) if seasons else None,
                user["id"],
                user["name"],
                now.isoformat(),
            ),
        )
    except sqlite3.IntegrityError:
        # The partial unique index caught what the SELECT above could not: a
        # second ask that arrived between that check and this insert. Same
        # answer either way -- the friend does not care which layer noticed.
        return JSONResponse({"error": _already_pending(settings)}, status_code=409)
    db.execute(
        _INSERT_EVENT,
        (now.isoformat(), user["name"], "discover_4k_requested", title),
    )
    await notify.owner_event(
        http, db, settings,
        title="🎞️ 4K request needs approval",
        body=f"{title} ({user['name']})",
    )
    return JSONResponse(
        {"ok": True, "state": "pending_approval", "id": cur.lastrowid, "title": title},
        status_code=202,
    )


@router.post("/request", response_model=None)
async def post_request(
    body: RequestBody,
    request: Request,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """File a Jellyseerr request as the signed-in friend.

    A 4K request from anyone but the owner takes the approval branch instead:
    a ``discover_4k_requests`` row plus a push at the owner, and *no* upstream
    call at all. Everything else -- including the owner's own 4K -- files
    immediately against the profile ``discover.profile_for`` picked.

    Returns:
        201 ``{"ok": True, "request_id": int | None, "title": str}`` when the
        request was filed. 202 ``{"ok": True, "state": "pending_approval",
        "id": int, "title": str}`` when a friend asked for 4K. 429 past the
        per-account hourly cap. 404 if the title doesn't exist. 409 with
        Jellyseerr's own wording on a duplicate, with the season refusal if
        the pick includes something already on the server or already
        requested, or if the same 4K request is already awaiting the owner.
        502 ``{"error": "couldn't map your account — ask <owner>"}`` if attribution
        fails, ``{"error": "720p lane not configured"}`` if the space-saver
        lane is unset on this deploy, or ``{"error": "jellyseerr
        unreachable"}`` -- which is also what a Jellyseerr 4xx *outside*
        ``_RELAYABLE_STATUSES`` (401/403/429) collapses to, so an upstream
        credential problem never reaches the browser as a 401.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    now = datetime.now(timezone.utc)
    needs_approval = body.quality == "4K" and user["role"] != "owner"

    # Resolved before the rate limiter on purpose: a deploy-side misconfig is
    # not the friend's doing and must not cost them one of their hourly slots.
    profile_id = discover.profile_for(settings, body.media_type, body.quality)
    if profile_id is None:
        return JSONResponse({"error": _NO_720_LANE}, status_code=502)

    if not request_limiter.check(f"discover-request:{user['id']}"):
        return JSONResponse(
            {"error": "That's a lot of requests in one hour — try again later."},
            status_code=429,
        )

    try:
        detail = await jellyseerr.media_detail(
            http, settings, body.media_type, body.tmdb_id, ttl=_DETAIL_TTL
        )
    except UpstreamError as exc:
        if exc.status == 404:
            return JSONResponse({"error": "not found"}, status_code=404)
        return _unreachable(exc)

    seasons: list[int] | None = None
    if body.media_type == "tv":
        seasons = sorted(set(body.seasons or []))
        refusal = discover.check_seasons(detail, seasons)
        if refusal is not None:
            return JSONResponse({"error": refusal}, status_code=409)

    title = detail["title"]

    if needs_approval:
        return await _file_for_approval(
            http, db, settings, body=body, user=user, title=title,
            seasons=seasons, now=now,
        )

    try:
        user_id = await discover.jellyseerr_user_id(http, settings, user["id"])
    except discover.UserMappingError:
        # Two very different causes wear the same exception. If plex.tv says
        # an invite is still sitting unaccepted, the friend can fix this
        # themselves and should be told how; anything else really is ours.
        # "unknown" deliberately falls through to the 502 — never guess a
        # friend into an instruction that may not apply.
        if await access.share_state(http, settings, user["id"]) == "pending":
            return JSONResponse({"error": _UNACCEPTED_INVITE}, status_code=409)
        return JSONResponse({"error": _unmapped(settings)}, status_code=502)
    except UpstreamError as exc:
        return _unreachable(exc)

    try:
        created = await jellyseerr.create_request(
            http,
            settings,
            media_type=body.media_type,
            tmdb_id=body.tmdb_id,
            user_id=user_id,
            seasons=seasons,
            profile_id=profile_id,
        )
    except UpstreamError as exc:
        # A relayable 4xx is Jellyseerr telling the *friend* something
        # actionable -- nearly always "you already asked for this". Its
        # wording beats anything invented here, so it is relayed verbatim
        # (bounded and 4xx-only, see ``clients/base._detail_of``). Any other
        # 4xx is clamped to the house 502: see ``_RELAYABLE_STATUSES``.
        if exc.status in _RELAYABLE_STATUSES:
            return JSONResponse(
                {"error": exc.detail or "Jellyseerr wouldn't take that request."},
                status_code=exc.status,
            )
        return _unreachable(exc)

    # Remember the title and artwork locally: the Pipeline board enriches from
    # the arr libraries, which don't contain a brand-new request yet (and the
    # library cache is minutes stale). Server-derived from the cached detail.
    db.execute(
        "INSERT OR REPLACE INTO title_hints (media_type, tmdb_id, title, poster)"
        " VALUES (?, ?, ?, ?)",
        (body.media_type, body.tmdb_id, title, detail.get("poster_path")),
    )
    scope = f" (seasons {', '.join(str(n) for n in seasons)})" if seasons else ""
    db.execute(
        _INSERT_EVENT,
        (now.isoformat(), user["name"], "media_requested", f"{title}{scope}"),
    )

    request_id = created.get("id") if isinstance(created, dict) else None
    return JSONResponse(
        {"ok": True, "request_id": request_id, "title": title}, status_code=201
    )
