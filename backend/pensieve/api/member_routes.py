"""Member-facing API routes (session-gated). Grows across Tasks 11-14.

``GET /api/storage`` is the first route: disk usage plus a Radarr/Sonarr
library breakdown. If either arr is unreachable, it falls back to the last
successfully cached response (via ``CachedHTTP.stale``) rather than failing
the whole view outright -- disk usage from ``os.statvfs`` has no upstream
dependency, so it's always live.

``GET /api/pipeline`` is the second route: a unified request/download board,
joining Jellyseerr requests against the Radarr/Sonarr download queues. Same
stale-cache fallback pattern as storage, but all three upstreams are equally
required -- there's no locally-computed value to keep serving on its own.

``GET /api/library/movies``, ``GET /api/library/series``, and
``GET /api/library/series/{arr_id}`` are the third group: the library browse
views. Unlike storage/pipeline, there's no stale-cache fallback here --
library browse isn't critical-path, so an upstream failure is just a 502
rather than serving old data. ``POST /api/library/refresh`` invalidates the
cached arr responses so the next browse GET refetches live.

``POST /api/flags``, ``GET /api/flags``, and ``POST /api/flags/{id}/veto``
are the fourth group: member deletion-flag actions, backed by
``services/deletion.py``'s state machine. Both write routes resolve the
media's title and size from ``arr_id`` against Radarr/Sonarr rather than
trusting the client's copies of them -- deletion executes on ``arr_id``, so a
client-supplied title could otherwise have the owner approving one film while
a different file comes off the disk. ``GET /api/flags`` opportunistically
calls ``deletion.sweep_expired`` on every read (not just the hourly owner-side
tick) so the 14-day veto window advances even between ticks, notifying the
owner of each flag that got swept into ``pending_approval``.

``POST /api/quality-requests`` and ``GET /api/quality-requests`` are the
fifth group: member-initiated quality upgrades. 1080p requests auto-trigger
(profile switch + arr search) with no gate; 4K requests always land in
``pending_approval`` for the owner. Owner notification fires for both
the 4K-approval-needed case and the swept deletion flags above -- Web Push
first, Discord only if that did not land, see ``notify.owner_event``.

``GET /api/push/public-key``, ``POST /api/push/subscribe`` and
``POST /api/push/unsubscribe`` are the sixth group (v0.3.0): the PWA's Web
Push registration. Nothing here notifies anybody -- they only record where a
browser can be reached, which ``pensieve/push.py`` then uses.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from pensieve import notify, push
from pensieve.auth import current_user
from pensieve.clients import jellyseerr, radarr, sonarr
from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.config import Settings
from pensieve.db import get_db
from pensieve.services import access, deletion, library, pipeline, quality, storage
from pensieve.services.deletion import FlagError

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


@router.get("/storage", response_model=None)
async def get_storage(request: Request) -> dict[str, Any] | JSONResponse:
    """Disk usage + library breakdown, with a stale-cache fallback.

    Returns:
        200 with the storage summary on success, or a stale-data 200 (with
        ``stale_seconds``) if Radarr/Sonarr are unreachable but a prior
        successful response is cached. 502 ``{"error": "<service> unreachable"}``
        if neither live nor cached data is available.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        return await storage.summary(http, settings)
    except UpstreamError as exc:
        return await _stale_fallback(http, settings, exc)


async def _stale_fallback(
    http: CachedHTTP, settings: Settings, exc: UpstreamError
) -> JSONResponse:
    """Best-effort storage summary built from cached (stale) arr responses.

    Disk usage is always live -- ``os.statvfs`` has no upstream dependency.
    Both the movie and series totals are required to build a full summary,
    so if either is missing from the cache, there's nothing useful to serve.
    """
    radarr_stale = http.stale(f"{settings.radarr_url}/api/v3/movie")
    sonarr_stale = http.stale(f"{settings.sonarr_url}/api/v3/series")

    if radarr_stale is None or sonarr_stale is None:
        return JSONResponse({"error": f"{exc.service} unreachable"}, status_code=502)

    raw_movies, movies_age = radarr_stale
    raw_series, series_age = sonarr_stale
    movies = radarr.shape_movies(raw_movies)
    series = sonarr.shape_series(raw_series)

    # Same hard-NFS hazard as ``storage.summary`` -- off the event loop.
    vfs = await run_in_threadpool(os.statvfs, settings.media_mount)
    total_bytes = vfs.f_frsize * vfs.f_blocks
    free_bytes = vfs.f_frsize * vfs.f_bavail
    used_bytes = total_bytes - free_bytes

    return JSONResponse(
        {
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "free_bytes": free_bytes,
            "movies_bytes": sum(m["size_bytes"] for m in movies),
            "tv_bytes": sum(s["size_bytes"] for s in series),
            "movie_count": len(movies),
            "series_count": len(series),
            "stale_seconds": max(movies_age, series_age),
        }
    )


_JELLYSEERR_PARAMS = {"take": 50, "sort": "added", "filter": "all"}
_RADARR_QUEUE_PARAMS = {"pageSize": 100, "includeUnknownMovieItems": "false"}
_SONARR_QUEUE_PARAMS = {"pageSize": 100}


@router.get("/pipeline", response_model=None)
async def get_pipeline(
    request: Request, db: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any] | JSONResponse:
    """Unified pipeline board: Jellyseerr requests joined with arr download queues.

    Returns:
        200 with ``{"cards": [...]}`` on success, or a stale-data 200 (with
        ``stale_seconds``) if an upstream is unreachable but prior successful
        responses are cached for all three. 502 ``{"error": "<service>
        unreachable"}`` if neither live nor cached data is available.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        requests = await jellyseerr.list_requests(http, settings)
        radarr_q = await radarr.get_queue(http, settings)
        sonarr_q = await sonarr.get_queue(http, settings)
    except UpstreamError as exc:
        return _pipeline_stale_fallback(http, settings, exc)

    # Best-effort title enrichment from the (cached) libraries -- jellyseerr
    # requests carry no titles. Never fail the board over enrichment.
    try:
        movies = await radarr.list_movies(http, settings, ttl=600)
        series = await sonarr.list_series(http, settings, ttl=600)
        hints = {
            (r["media_type"], r["tmdb_id"]): {"title": r["title"], "poster": r["poster"]}
            for r in db.execute(
                "SELECT media_type, tmdb_id, title, poster FROM title_hints"
            ).fetchall()
        }
        requests = pipeline.enrich_media(
            requests,
            movie_titles={m["tmdb_id"]: m["title"] for m in movies if m.get("tmdb_id")},
            series_titles={s["tvdb_id"]: s["title"] for s in series if s.get("tvdb_id")},
            movie_posters={
                m["tmdb_id"]: m["poster"] for m in movies if m.get("tmdb_id") and m.get("poster")
            },
            series_posters={
                s["tvdb_id"]: s["poster"] for s in series if s.get("tvdb_id") and s.get("poster")
            },
            hints=hints,
        )
    except Exception:  # noqa: BLE001 -- enrichment is best-effort by design
        pass

    cards = pipeline.build(requests, radarr_q, sonarr_q, now=datetime.now(timezone.utc))
    return {"cards": cards}


def _pipeline_stale_fallback(http: CachedHTTP, settings: Settings, exc: UpstreamError) -> JSONResponse:
    """Best-effort pipeline board built from cached (stale) upstream responses.

    All three upstreams are equally required to build a meaningful board --
    unlike storage, there's no locally-computed value to keep serving on its
    own -- so if any one is missing from the cache, there's nothing useful
    to return.
    """
    jellyseerr_stale = http.stale(f"{settings.jellyseerr_url}/api/v1/request", _JELLYSEERR_PARAMS)
    radarr_stale = http.stale(f"{settings.radarr_url}/api/v3/queue", _RADARR_QUEUE_PARAMS)
    sonarr_stale = http.stale(f"{settings.sonarr_url}/api/v3/queue", _SONARR_QUEUE_PARAMS)

    if jellyseerr_stale is None or radarr_stale is None or sonarr_stale is None:
        return JSONResponse({"error": f"{exc.service} unreachable"}, status_code=502)

    raw_requests, requests_age = jellyseerr_stale
    raw_radarr_q, radarr_age = radarr_stale
    raw_sonarr_q, sonarr_age = sonarr_stale

    requests = jellyseerr.shape_requests(raw_requests)
    radarr_q = radarr.shape_queue(raw_radarr_q)
    sonarr_q = sonarr.shape_queue(raw_sonarr_q)

    cards = pipeline.build(requests, radarr_q, sonarr_q, now=datetime.now(timezone.utc))
    return JSONResponse(
        {"cards": cards, "stale_seconds": max(requests_age, radarr_age, sonarr_age)}
    )


@router.get("/library/movies", response_model=None)
async def get_library_movies(request: Request) -> dict[str, Any] | JSONResponse:
    """Movie library browse list.

    Returns:
        200 ``{"items": [...]}`` on success. 502 ``{"error": "<service>
        unreachable"}`` if Radarr is unreachable -- no stale-cache fallback,
        see module docstring.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        raw = await radarr.list_movies(http, settings)
    except UpstreamError as exc:
        return JSONResponse({"error": f"{exc.service} unreachable"}, status_code=502)

    return {"items": library.movies(raw)}


@router.get("/library/series", response_model=None)
async def get_library_series(request: Request) -> dict[str, Any] | JSONResponse:
    """Series library browse list (rollup rows, no per-season quality mix).

    Returns:
        200 ``{"items": [...]}`` on success. 502 ``{"error": "<service>
        unreachable"}`` if Sonarr is unreachable -- no stale-cache fallback,
        see module docstring.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        raw = await sonarr.list_series(http, settings)
    except UpstreamError as exc:
        return JSONResponse({"error": f"{exc.service} unreachable"}, status_code=502)

    return {"items": library.series_list(raw)}


@router.get("/library/series/{arr_id}", response_model=None)
async def get_library_series_detail(arr_id: int, request: Request) -> dict[str, Any] | JSONResponse:
    """Single series detail, with per-season quality mix.

    Returns:
        200 with the series_detail-shaped row on success. 404 ``{"error":
        "series not found"}`` if ``arr_id`` isn't in the Sonarr library. 502
        ``{"error": "<service> unreachable"}`` if Sonarr is unreachable.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    try:
        series = await sonarr.list_series(http, settings)
        row = next((s for s in series if s["arr_id"] == arr_id), None)
        if row is None:
            return JSONResponse({"error": "series not found"}, status_code=404)
        files = await sonarr.episode_files(http, settings, arr_id, ttl=600)
    except UpstreamError as exc:
        return JSONResponse({"error": f"{exc.service} unreachable"}, status_code=502)

    return library.series_detail(row, files)


@router.post("/library/refresh", response_model=None)
async def post_library_refresh(request: Request) -> dict[str, Any]:
    """Invalidate cached Radarr/Sonarr library responses, forcing a live refetch.

    A local cache operation only -- no upstream call is made, so there's
    nothing here that can fail.

    Returns:
        ``{"ok": True}``.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    http.invalidate(f"{settings.radarr_url}/api/v3/movie")
    http.invalidate(f"{settings.sonarr_url}/api/v3/series")
    http.invalidate(f"{settings.sonarr_url}/api/v3/episodefile")

    return {"ok": True}


class FlagCreateBody(BaseModel):
    """Request body for ``POST /api/flags``.

    ``title`` and ``size_bytes`` are accepted (the frontend still sends them)
    but **ignored**: both are resolved server-side from ``arr_id`` — see
    ``_resolve_media``.
    """

    media_type: Literal["movie", "series"]
    arr_id: int = Field(ge=1)
    season_number: int | None = Field(default=None, ge=0, le=1000)
    title: str | None = Field(default=None, max_length=300)
    size_bytes: int = 0
    reason: str | None = Field(default=None, max_length=1000)


class QualityRequestBody(BaseModel):
    """Request body for ``POST /api/quality-requests``.

    ``title`` is accepted but ignored, same as on ``FlagCreateBody``.
    """

    media_type: Literal["movie", "series"]
    arr_id: int = Field(ge=1)
    season_number: int | None = Field(default=None, ge=0, le=1000)
    title: str | None = Field(default=None, max_length=300)
    requested: Literal["1080p", "4K"]
    current_quality: str | None = Field(default=None, max_length=100)


class MediaNotFound(Exception):
    """Raised when ``arr_id`` (or a season within it) doesn't exist upstream."""

    def __init__(self, detail: str) -> None:
        """Initialize with the client-facing error string ("not found", ...)."""
        super().__init__(detail)
        self.detail = detail


async def _fetch_item(
    http: CachedHTTP, settings: Settings, media_type: str, arr_id: int
) -> dict[str, Any]:
    """Fetch the raw arr object for ``arr_id``, uncached.

    Raises:
        MediaNotFound: If the arr says there is no such item (404).
        UpstreamError: For any other upstream failure — "radarr is down" and
            "that movie doesn't exist" must not collapse into one answer.
    """
    try:
        if media_type == "movie":
            return await radarr.get_movie(http, settings, arr_id)
        return await sonarr.get_series(http, settings, arr_id)
    except UpstreamError as exc:
        if exc.status == 404:
            raise MediaNotFound("not found") from exc
        raise


def _resolve_media(
    item: dict[str, Any], media_type: str, season_number: int | None
) -> tuple[str, int]:
    """Derive the authoritative ``(title, size_bytes)`` for a flag scope.

    The client supplies ``arr_id``, ``title`` and ``size_bytes`` as three
    independent fields, but deletion executes on ``arr_id`` alone — so a
    mismatch means the owner approves one title and Radarr/Sonarr delete a
    different file. The stored row and the Discord notification therefore use
    what the arr says about ``arr_id``, never what the client claimed.

    Raises:
        MediaNotFound: If ``season_number`` isn't a season of this series.
    """
    title = item.get("title") or f"{media_type} {item.get('id')}"

    if media_type == "movie":
        movie_file = item.get("movieFile") or {}
        return title, int(movie_file.get("size") or item.get("sizeOnDisk") or 0)

    if season_number is None:
        return title, int((item.get("statistics") or {}).get("sizeOnDisk") or 0)

    season = next(
        (s for s in item.get("seasons", []) if s.get("seasonNumber") == season_number), None
    )
    if season is None:
        raise MediaNotFound("season not found")
    return title, int((season.get("statistics") or {}).get("sizeOnDisk") or 0)


_INSERT_QUALITY_REQUEST = """
INSERT INTO quality_requests (media_type, arr_id, season_number, title,
    current_quality, requested_quality, state, requested_by,
    requested_by_name, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_QUALITY_REQUEST_ERROR = """
UPDATE quality_requests SET state = 'error', error = ? WHERE id = ?
"""

_INSERT_EVENT = "INSERT INTO events (at, actor, action, detail) VALUES (?, ?, ?, ?)"

# A request for the same (media_type, arr_id, season_number, requested_quality)
# scope is a duplicate if one is still awaiting owner approval (no expiry --
# it hasn't been decided yet), or if an auto-triggered one already fired the
# arr search within the dedupe window (no point re-triggering the same search).
_QUALITY_REQUEST_DEDUPE_WINDOW_HOURS = 24

_DUPLICATE_QUALITY_REQUEST_CHECK = """
SELECT id FROM quality_requests
WHERE media_type = ? AND arr_id = ? AND IFNULL(season_number, -1) = IFNULL(?, -1)
    AND requested_quality = ?
    AND (state = 'pending_approval' OR (state = 'auto_triggered' AND created_at > ?))
"""


@router.post("/flags", response_model=None)
async def post_flag(
    body: FlagCreateBody,
    request: Request,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Create a deletion flag for a media scope.

    The stored ``title``/``size_bytes`` are resolved from ``arr_id`` against
    Radarr/Sonarr; the body's values (still accepted, for the existing
    frontend) are ignored.

    Returns:
        201 with the created flag row on success. 404 ``{"error": "not
        found"}`` if ``arr_id`` isn't in the arr, or ``{"error": "season not
        found"}`` if the series has no such season. 409 ``{"error":
        str(exc)}`` if an active flag already exists for this scope, or a
        vetoed/denied flag was resolved within the reflag cooldown. 502
        ``{"error": "<service> unreachable"}`` if the arr lookup fails.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    now = datetime.now(timezone.utc)

    try:
        item = await _fetch_item(http, settings, body.media_type, body.arr_id)
        title, size_bytes = _resolve_media(item, body.media_type, body.season_number)
    except MediaNotFound as exc:
        return JSONResponse({"error": exc.detail}, status_code=404)
    except UpstreamError as exc:
        return JSONResponse({"error": f"{exc.service} unreachable"}, status_code=502)

    try:
        flag = deletion.create_flag(
            db,
            media_type=body.media_type,
            arr_id=body.arr_id,
            season_number=body.season_number,
            title=title,
            size_bytes=size_bytes,
            reason=body.reason,
            by_id=user["id"],
            by_name=user["name"],
            now=now,
        )
    except FlagError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    # Everyone but the flagger, and no Discord fallback: the veto window is a
    # household matter, and the person who just pressed Flag does not need
    # telling. A failure here is swallowed inside ``push`` -- the flag is
    # already created, and an undelivered notification must not undo it.
    await push.broadcast(
        db,
        settings,
        {
            "title": "Marked for deletion",
            "body": f"{title} — {deletion.VETO_WINDOW_DAYS} days to veto",
            "tab": "flagged",
        },
        exclude=user["id"],
    )
    return JSONResponse(flag, status_code=201)


def _without_error(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the ``error`` column from member-facing deletion-flag rows.

    ``deletion.list_flags`` is a ``SELECT *``, and ``error`` holds the raw
    upstream exception text (internal hostnames, ports, API paths) written by
    ``mark_error``. ``approved`` rows are member-visible, so a failed
    execution would otherwise hand every friend an internal-topology readout.
    The owner's admin queue keeps the column.
    """
    return [{k: v for k, v in row.items() if k != "error"} for row in rows]


@router.get("/flags", response_model=None)
async def get_flags(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """Active + recent deletion flags, sweeping expired flags first.

    Sweeping here (in addition to the hourly owner-side tick, Task 15) means
    the 14-day veto window advances on read too, so a flag doesn't sit stale
    until the next tick just because nobody looked at the owner view. Each
    flag moved into ``pending_approval`` fires a Discord notification.

    Returns:
        ``{"active": [...flagged...], "recent": [...last 20 resolved...]}``.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    now = datetime.now(timezone.utc)

    moved = deletion.sweep_expired(db, now)
    for row in moved:
        await notify.owner_event(
            http,
            db,
            settings,
            title="🗑️ Deletion approval needed",
            body=f"{row['title']} — flagged by {row['flagged_by_name']} "
                 "14 days ago, no vetoes",
        )

    return {
        "active": _without_error(deletion.list_flags(db, ["flagged"])),
        "recent": _without_error(
            deletion.list_flags(
                db, ["vetoed", "pending_approval", "approved", "denied", "executed"]
            )[:20]
        ),
    }


@router.post("/flags/{flag_id}/veto", response_model=None)
async def post_flag_veto(
    flag_id: int,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Veto a deletion flag, closing it out.

    Returns:
        200 with the updated flag row on success. 409 ``{"error": str(exc)}``
        if the flag isn't in the ``flagged`` state (e.g. already vetoed).
    """
    now = datetime.now(timezone.utc)
    try:
        flag = deletion.veto_flag(db, flag_id, user["name"], now)
    except FlagError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return flag


@router.post("/quality-requests", response_model=None)
async def post_quality_request(
    body: QualityRequestBody,
    request: Request,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Request a quality upgrade for a movie or series.

    1080p requests auto-trigger: an optional profile switch, then an arr
    search (season-scoped when ``season_number`` is set on a series,
    otherwise whole-item). 4K requests always land in ``pending_approval``
    for the owner, with no arr calls made at all.

    Returns:
        409 ``{"error": "duplicate request"}`` if a request for the same
        scope + resolution is already ``pending_approval``, or was
        ``auto_triggered`` within the last 24h. 200 ``{"state":
        "pending_approval", "id": ...}`` for a 4K request. 200 ``{"state":
        "auto_triggered", "id": ...}`` for a successful 1080p auto-trigger.
        502 ``{"state": "error", "error": "<service> unreachable"}`` if an
        arr call fails during auto-trigger (the row is left in state
        ``error`` with the full exception text, for the owner's Approvals
        view -- only the sanitized service name reaches this response). 502
        ``{"error": "<service> unreachable"}`` if the initial profile-id
        lookup fails.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    now = datetime.now(timezone.utc)

    dedupe_cutoff = now - timedelta(hours=_QUALITY_REQUEST_DEDUPE_WINDOW_HOURS)
    duplicate = db.execute(
        _DUPLICATE_QUALITY_REQUEST_CHECK,
        (body.media_type, body.arr_id, body.season_number, body.requested,
         dedupe_cutoff.isoformat()),
    ).fetchone()
    if duplicate is not None:
        return JSONResponse({"error": "duplicate request"}, status_code=409)

    try:
        item = await _fetch_item(http, settings, body.media_type, body.arr_id)
        title, _size_bytes = _resolve_media(item, body.media_type, body.season_number)
    except MediaNotFound as exc:
        return JSONResponse({"error": exc.detail}, status_code=404)
    except UpstreamError as exc:
        return JSONResponse({"error": f"{exc.service} unreachable"}, status_code=502)

    current_profile_id = item["qualityProfileId"]
    four_k_id = (
        settings.radarr_profile_4k_id if body.media_type == "movie"
        else settings.sonarr_profile_4k_id
    )
    # A 1080p request on a 4K item is a downgrade, and 1080p is the
    # auto-executing tier: it would switch the profile down and immediately
    # search, which can replace the existing 4K file on import. Nobody
    # legitimately asks for worse, so refuse rather than route it for
    # approval -- no row is written, so there is nothing to retry either.
    if body.requested == "1080p" and current_profile_id == four_k_id:
        return JSONResponse(
            {"error": "already 4K — downgrades aren't supported"}, status_code=409
        )

    plan = quality.plan_action(
        media_type=body.media_type,
        requested=body.requested,
        current_profile_id=current_profile_id,
        settings=settings,
    )
    state = "pending_approval" if plan["tier"] == "approval" else "auto_triggered"

    cur = db.execute(
        _INSERT_QUALITY_REQUEST,
        (
            body.media_type, body.arr_id, body.season_number, title,
            body.current_quality, body.requested, state,
            user["id"], user["name"], now.isoformat(),
        ),
    )
    request_id = cur.lastrowid
    db.execute(
        _INSERT_EVENT,
        (now.isoformat(), user["name"], "quality_requested",
         f"{title}: {body.requested}"),
    )

    if plan["tier"] == "approval":
        await notify.owner_event(
            http, db, settings,
            title="🎞️ 4K request needs approval",
            body=f"{title} ({user['name']})",
        )
        return {"state": "pending_approval", "id": request_id}

    try:
        if plan["needs_profile_switch"]:
            if body.media_type == "movie":
                await radarr.set_profile(http, settings, body.arr_id, plan["target_profile_id"])
            else:
                await sonarr.set_profile(http, settings, body.arr_id, plan["target_profile_id"])

        if body.media_type == "movie":
            await radarr.search_movie(http, settings, body.arr_id)
        elif body.season_number is not None:
            await sonarr.search_season(http, settings, body.arr_id, body.season_number)
        else:
            await sonarr.search_series(http, settings, body.arr_id)
    except UpstreamError as exc:
        # Full exception text (may include internal hostnames/ports) is kept
        # on the DB row for the owner's Approvals view; the friend-facing
        # HTTP response only ever gets the sanitized service name.
        db.execute(_UPDATE_QUALITY_REQUEST_ERROR, (str(exc), request_id))
        return JSONResponse(
            {"state": "error", "error": f"{exc.service} unreachable"}, status_code=502
        )

    return {"state": "auto_triggered", "id": request_id}


_LIST_QUALITY_REQUESTS = """
SELECT id, media_type, arr_id, season_number, title, current_quality,
    requested_quality, state, requested_by, requested_by_name, created_at,
    resolved_at, note
FROM quality_requests ORDER BY created_at DESC, id DESC LIMIT 30
"""


@router.get("/quality-requests", response_model=None)
async def get_quality_requests(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """Newest 30 quality requests, across all users.

    Deliberately excludes the ``error`` column -- it can hold the full
    upstream exception text (internal hostnames/ports), which is fine for
    the DB record but must never reach a friend-facing response. ``state``
    still reports ``'error'`` so the UI can show a failed badge; the detail
    itself is owner-only (Task 15's admin queue).

    Returns:
        ``{"items": [...]}``.
    """
    rows = db.execute(_LIST_QUALITY_REQUESTS).fetchall()
    return {"items": [dict(r) for r in rows]}


# --- Web Push registration (v0.3.0) -------------------------------------------

#: Generous but finite bounds. Real endpoints run ~150-500 chars (FCM, Mozilla,
#: WNS); the keys are fixed-size base64url (65 and 16 raw bytes). These exist so
#: an authenticated member cannot park megabytes of arbitrary text in the table.
_MAX_ENDPOINT = 1000
_MAX_P256DH = 200
_MAX_AUTH = 100


class PushKeys(BaseModel):
    """The ECDH/auth pair a browser hands out with its subscription."""

    p256dh: str = Field(min_length=1, max_length=_MAX_P256DH)
    auth: str = Field(min_length=1, max_length=_MAX_AUTH)


class PushSubscriptionBody(BaseModel):
    """A browser ``PushSubscription``, as ``subscription.toJSON()`` emits it."""

    endpoint: str = Field(min_length=1, max_length=_MAX_ENDPOINT)
    keys: PushKeys

    @field_validator("endpoint")
    @classmethod
    def _https_only(cls, value: str) -> str:
        """Refuse anything that is not an https push endpoint.

        The server later POSTs to whatever it stored here, so an unconstrained
        endpoint turns any signed-in member into a request forwarder aimed at
        the LAN. Every real push service is https, so this costs nothing.
        """
        if not value.startswith("https://"):
            raise ValueError("endpoint must be an https:// URL")
        return value


class PushSubscribeBody(BaseModel):
    """Request body for ``POST /api/push/subscribe``."""

    subscription: PushSubscriptionBody


class PushUnsubscribeBody(BaseModel):
    """Request body for ``POST /api/push/unsubscribe``."""

    endpoint: str = Field(min_length=1, max_length=_MAX_ENDPOINT)


@router.get("/push/public-key", response_model=None)
async def get_push_public_key(request: Request) -> dict[str, str]:
    """The VAPID application server key the browser subscribes with.

    Returns:
        ``{"key": "<base64url>"}``, or ``{"key": ""}`` when push is not
        configured -- the UI reads an empty key as "hide the toggle" rather
        than as an error, so an unconfigured deploy just has no bell.
    """
    settings: Settings = request.app.state.settings
    return {"key": settings.vapid_public_key}


@router.post("/push/subscribe", response_model=None)
async def post_push_subscribe(
    body: PushSubscribeBody,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """Register (or refresh) this browser's push endpoint for the session user.

    Idempotent: re-subscribing the same endpoint updates the existing row
    rather than failing on the ``UNIQUE`` constraint, which is what a browser
    does routinely when its subscription rotates.

    Returns:
        201 ``{"ok": True}``.
    """
    now = datetime.now(timezone.utc)
    await push.subscribe(
        db, user_id=user["id"], subscription=body.subscription.model_dump(), now=now
    )
    db.execute(
        _INSERT_EVENT,
        (now.isoformat(), user["name"], "push_subscribed", body.subscription.endpoint[:120]),
    )
    return JSONResponse({"ok": True}, status_code=201)


@router.post("/push/unsubscribe", response_model=None)
async def post_push_unsubscribe(
    body: PushUnsubscribeBody,
    user: dict = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Forget this browser's push endpoint.

    Scoped to the session account, so knowing another person's endpoint URL
    cannot mute their notifications. A miss answers 200 all the same -- the
    caller asked for "not subscribed", and that is the state either way.

    Returns:
        ``{"ok": True}``.
    """
    await push.unsubscribe(db, body.endpoint, plex_account_id=user["id"])
    return {"ok": True}


@router.get("/me/share", response_model=None)
async def get_my_share_state(
    request: Request, user: dict = Depends(current_user)
) -> dict[str, str]:
    """Where the signed-in member stands with the Plex server itself.

    Exists because being a member here and being on the Plex server stopped being
    the same thing in 0.5.2: an approved friend can browse everything and
    still have no Jellyseerr user, so every request fails. The UI reads this
    to say so up front rather than after the fact.

    Returns:
        200 ``{"state": "active" | "pending" | "none" | "unknown"}``. Never
        an error -- ``unknown`` is what an unreadable plex.tv looks like, and
        a card that explains a problem must not become one.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    return {"state": await access.share_state(http, settings, user["id"])}
