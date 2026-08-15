"""Owner-only admin API routes (session-gated, ``role == "owner"``).

Every route in this router requires the owner role via ``require_owner`` --
a member session gets a 403 on all of them. This is where the two approval
queues from Task 14 (deletion flags and 4K quality requests) get their real
teeth: approving here actually calls Radarr/Sonarr to delete files or switch
profiles + search, not just flip a database row.

``GET /api/admin/queue`` is the combined dashboard: pending/approved
deletion flags, plus quality requests that are either awaiting approval or
sitting in ``error`` from a failed execution attempt. Unlike the member-facing
``GET /api/quality-requests`` (Task 14), this includes the raw ``error``
column -- the sanitized-away upstream exception text lives here, for the
owner's eyes only.

``POST /api/admin/flags/{id}/approve`` and ``POST /api/admin/quality/{id}/approve``
both support a retry: if a prior approve call resolved the row but then
failed during the actual Radarr/Sonarr call (state left in ``approved`` /
``error`` with the failure recorded), calling approve again skips straight
to re-executing rather than re-resolving (which would raise, since
``resolve_flag`` only accepts a ``pending_approval`` source state).

``POST /api/admin/access-requests/{id}/approve`` is the third queue (v0.2.0):
approving calls the plex.tv share API to invite the account onto the server
and creates its ``users`` row, so a friend can be let in from this screen
without touching Plex itself.

``POST /api/admin/discover-4k/{id}/approve|deny`` is the fourth queue
(v0.5.0): a friend asking for 4K in Discover files nothing upstream, it files
a row here. Both outcomes reach Jellyseerr -- approve files the 4K profile,
**deny still files the title at 1080p**. Turning down 4K means "not at that
size", not "you cannot watch it", and silently dropping the request would
leave a friend waiting on a download that was never coming. Either way the
request is attributed to the *friend's* Jellyseerr user, never the owner's.

``GET /api/admin/users`` plus the ``revoke``/``unrevoke`` pair are the kill
switch behind ``pensieve.auth.current_user``'s per-request database check:
flipping ``users.revoked`` cuts a session off on its next request, without
rotating ``SESSION_SECRET`` (which would log out everyone, owner included).
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pensieve import push
from pensieve.auth import require_owner
from pensieve.clients import jellyseerr, plex_tv, radarr, sonarr
from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.config import Settings
from pensieve.db import get_db
from pensieve.services import deletion, discover, quality
from pensieve.services.deletion import FlagError

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_owner)])

_INSERT_EVENT = "INSERT INTO events (at, actor, action, detail) VALUES (?, ?, ?, ?)"

_LIST_QUALITY_QUEUE = """
SELECT * FROM quality_requests WHERE state IN ('pending_approval', 'error')
ORDER BY created_at DESC
"""

_UPDATE_QUALITY_APPROVED = """
UPDATE quality_requests SET state = 'approved', error = NULL, resolved_at = ? WHERE id = ?
"""

_UPDATE_QUALITY_ERROR = "UPDATE quality_requests SET state = 'error', error = ? WHERE id = ?"

_UPDATE_QUALITY_DENIED = """
UPDATE quality_requests SET state = 'denied', note = ?, resolved_at = ? WHERE id = ?
"""

_LIST_DISCOVER_4K_QUEUE = """
SELECT * FROM discover_4k_requests WHERE state = 'pending' ORDER BY created_at
"""

#: ``AND state = 'pending'`` is the compare-and-swap: ``_load_pending_4k``
#: reads the state several awaits before this runs, so two owner sessions
#: (phone and laptop) can both pass the gate. Losing that race must be a
#: no-op, not a second contradictory settlement -- callers check ``rowcount``.
_UPDATE_DISCOVER_4K = """
UPDATE discover_4k_requests SET state = ?, note = ?, resolved_at = ?
WHERE id = ? AND state = 'pending'
"""

#: Answer when the compare-and-swap above finds the row already settled.
_ALREADY_SETTLED = "that 4K request was just resolved somewhere else"

_LIST_ACCESS_QUEUE = """
SELECT * FROM access_requests WHERE state = 'pending' ORDER BY created_at
"""

_UPDATE_ACCESS_APPROVED = """
UPDATE access_requests SET state = 'approved', resolved_at = ? WHERE id = ?
"""

_UPDATE_ACCESS_DENIED = """
UPDATE access_requests SET state = 'denied', note = ?, resolved_at = ? WHERE id = ?
"""

# Approving is also the only way a member row gets created without a login,
# so an account revoked earlier and re-approved now comes back un-revoked.
# `role` is deliberately left alone on conflict: re-approving must never
# demote an existing owner.
_UPSERT_APPROVED_MEMBER = """
INSERT INTO users (plex_account_id, name, role, last_seen, revoked)
VALUES (?, ?, 'member', ?, 0)
ON CONFLICT(plex_account_id) DO UPDATE SET revoked = 0
"""


def _log_event(db: sqlite3.Connection, now: datetime, actor: str, action: str, detail: str) -> None:
    """Append a row to the events table for an owner admin action."""
    db.execute(_INSERT_EVENT, (now.isoformat(), actor, action, detail))


async def _notify_requester(
    db: sqlite3.Connection, settings: Settings, row: sqlite3.Row, *,
    approved: bool, note: str | None = None,
) -> None:
    """Push a quality-request outcome back to whoever asked for it.

    Push only, with no Discord fallback: this is a message *to a member*, and
    the Discord webhook is the owner's channel -- falling back there would
    tell the wrong person. Someone with no subscription simply finds out on
    the Flagged view, which is where the outcome already lived.
    """
    await push.send_to_user(
        db,
        settings,
        row["requested_by"],
        {
            "title": "Request approved" if approved else "Request declined",
            "body": f"{row['title']} — {note}" if note else row["title"],
            "tab": "flagged",
        },
    )


def _invalidate_library_caches(http: CachedHTTP, settings: Settings) -> None:
    """Drop cached Radarr/Sonarr library responses after a deletion executes."""
    http.invalidate(f"{settings.radarr_url}/api/v3/movie")
    http.invalidate(f"{settings.sonarr_url}/api/v3/series")
    http.invalidate(f"{settings.sonarr_url}/api/v3/episodefile")


async def _waiting_on_plex(
    http: CachedHTTP, settings: Settings, db: sqlite3.Connection
) -> list[dict[str, Any]]:
    """Approved members whose Plex invite is still unaccepted.

    Derived from live plex.tv state rather than stored, so it corrects itself
    the moment someone accepts -- there is no row to clean up and nothing to
    keep in sync.

    Returns an empty list if plex.tv cannot be read. This is the least
    important thing on the queue and must never be able to take down the four
    that were already there.
    """
    try:
        invites = {
            invite["id"]: invite
            for invite in await plex_tv.list_pending_invites(http, settings)
        }
    except UpstreamError:
        return []

    rows = db.execute(
        "SELECT plex_account_id, name FROM users WHERE revoked = 0 AND role != 'owner'"
    ).fetchall()
    return [
        {
            "plex_account_id": row["plex_account_id"],
            "name": row["name"],
            "email": invites[row["plex_account_id"]]["email"],
            "invited_at": invites[row["plex_account_id"]]["invited_at"],
        }
        for row in rows
        if row["plex_account_id"] in invites
    ]


@router.get("/queue", response_model=None)
async def get_admin_queue(
    request: Request, db: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    """Combined owner approval queue: deletions, quality, access, 4K asks.

    Returns:
        ``{"deletions": [...pending_approval/approved flags...], "quality":
        [...pending_approval/error quality requests, including the raw
        "error" column...], "access": [...pending access requests, oldest
        first...], "discover_4k": [...pending Discover 4K requests, oldest
        first...]}``.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http

    deletions = deletion.list_flags(db, ["pending_approval", "approved"])
    quality_rows = db.execute(_LIST_QUALITY_QUEUE).fetchall()
    access_rows = db.execute(_LIST_ACCESS_QUEUE).fetchall()
    discover_4k_rows = db.execute(_LIST_DISCOVER_4K_QUEUE).fetchall()
    return {
        "deletions": deletions,
        "quality": [dict(r) for r in quality_rows],
        "access": [dict(r) for r in access_rows],
        "discover_4k": [dict(r) for r in discover_4k_rows],
        "waiting_on_plex": await _waiting_on_plex(http, settings, db),
    }


@router.post("/flags/{flag_id}/approve", response_model=None)
async def post_flag_approve(
    flag_id: int,
    request: Request,
    user: dict = Depends(require_owner),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Approve a deletion flag and execute the deletion against Radarr/Sonarr.

    From ``pending_approval``, resolves the flag to ``approved`` first. From
    ``approved`` (a prior execution attempt failed), skips straight to
    re-execution -- this is the retry path, since ``resolve_flag`` only
    accepts a ``pending_approval`` source state. Any other state is a 409.

    Returns:
        200 ``{"state": "executed"}`` on success (also invalidates the
        Radarr/Sonarr library caches). 404 if the flag doesn't exist. 409
        ``{"error": ...}`` if the flag isn't in an approvable state. 502
        ``{"state": "approved", "error": "<service> unreachable"}`` if the
        Radarr/Sonarr call fails -- the flag stays ``approved`` (with the
        full exception recorded on the row) so this endpoint can be retried.
        200 ``{"state": "executed"}`` can also be returned from the
        ``UpstreamError`` branch if a concurrent request already executed
        this same flag between our fetch and the failed call (see the
        ``mark_error`` handling below).
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    now = datetime.now(timezone.utc)

    row = db.execute("SELECT * FROM deletion_flags WHERE id = ?", (flag_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": f"no deletion flag with id {flag_id}"}, status_code=404)

    if row["state"] == "pending_approval":
        try:
            flag = deletion.resolve_flag(db, flag_id, "approved", None, now)
        except FlagError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    elif row["state"] == "approved":
        flag = dict(row)  # retry: already resolved, re-execute below
    else:
        return JSONResponse(
            {"error": f"flag {flag_id} is in state {row['state']!r}, not approvable"},
            status_code=409,
        )

    _log_event(db, now, user["name"], "flag_approved", f"flag {flag_id}: {flag['title']}")

    try:
        if flag["media_type"] == "movie":
            await radarr.delete_movie(http, settings, flag["arr_id"])
        elif flag["season_number"] is None:
            await sonarr.delete_series(http, settings, flag["arr_id"])
        else:
            await sonarr.delete_season(http, settings, flag["arr_id"], flag["season_number"])
    except UpstreamError as exc:
        try:
            deletion.mark_error(db, flag_id, str(exc))
        except FlagError:
            # Another request already resolved this flag (most likely it
            # executed successfully) between our fetch above and this write
            # -- mark_error only accepts 'approved' as a source state. Don't
            # report a failure that's no longer true; re-check what actually
            # happened instead. No mark_error write occurred in this branch
            # (it raises before writing), so there's nothing to undo.
            current = db.execute(
                "SELECT state FROM deletion_flags WHERE id = ?", (flag_id,)
            ).fetchone()
            if current is not None and current["state"] == "executed":
                return {"state": "executed"}
        return JSONResponse(
            {"state": "approved", "error": f"{exc.service} unreachable"}, status_code=502
        )

    deletion.mark_executed(db, flag_id, now)
    _invalidate_library_caches(http, settings)

    return {"state": "executed"}


class FlagDenyBody(BaseModel):
    """Request body for ``POST /api/admin/flags/{flag_id}/deny``."""

    note: str | None = Field(default=None, max_length=1000)


@router.post("/flags/{flag_id}/deny", response_model=None)
async def post_flag_deny(
    flag_id: int,
    body: FlagDenyBody = FlagDenyBody(),
    user: dict = Depends(require_owner),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Deny a deletion flag, closing it out with no arr calls made.

    Returns:
        200 ``{"state": "denied"}`` on success. 404 if the flag doesn't
        exist. 409 ``{"error": ...}`` if the flag isn't in
        ``pending_approval``.
    """
    now = datetime.now(timezone.utc)

    row = db.execute("SELECT * FROM deletion_flags WHERE id = ?", (flag_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": f"no deletion flag with id {flag_id}"}, status_code=404)

    try:
        deletion.resolve_flag(db, flag_id, "denied", body.note, now)
    except FlagError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    _log_event(db, now, user["name"], "flag_denied", f"flag {flag_id}: {body.note or ''}")
    return {"state": "denied"}


@router.post("/quality/{req_id}/approve", response_model=None)
async def post_quality_approve(
    req_id: int,
    request: Request,
    user: dict = Depends(require_owner),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Approve a quality request: switch to the requested profile and search.

    Allowed from ``pending_approval`` (first approval) or ``error`` (a prior
    approve call resolved the profile switch/search but failed partway --
    retrying re-runs the same set_profile + search). Any other state
    (``auto_triggered``, ``approved``, ``denied``) is a 409.

    Returns:
        200 ``{"state": "approved"}`` on success. 404 if the request doesn't
        exist. 409 ``{"error": ...}`` if not in an approvable state. 502
        ``{"state": "error", "error": "<service> unreachable"}`` if the
        Radarr/Sonarr call fails -- the row is left in ``error`` (full
        exception text on the row) so this endpoint can be retried.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    now = datetime.now(timezone.utc)

    row = db.execute("SELECT * FROM quality_requests WHERE id = ?", (req_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": f"no quality request with id {req_id}"}, status_code=404)
    if row["state"] not in ("pending_approval", "error"):
        return JSONResponse(
            {"error": f"quality request {req_id} is in state {row['state']!r}, not approvable"},
            status_code=409,
        )

    media_type = row["media_type"]
    arr_id = row["arr_id"]
    season_number = row["season_number"]
    # The target follows what was ASKED FOR, not just the media type. Every
    # `error` row is by construction a 1080p request (error is only written
    # inside the member route's 1080p-only auto-trigger block), and Retry is
    # the only button the Approvals UI renders for those rows -- so keying
    # off media_type alone would escalate every retry to 4K and fire a 4K
    # search while the UI says "Searching for a 1080p copy".
    target_profile_id = quality.target_profile_id(
        media_type=media_type, requested=row["requested_quality"], settings=settings
    )

    _log_event(db, now, user["name"], "quality_approved", f"request {req_id}: {row['title']}")

    try:
        if media_type == "movie":
            await radarr.set_profile(http, settings, arr_id, target_profile_id)
            await radarr.search_movie(http, settings, arr_id)
        else:
            await sonarr.set_profile(http, settings, arr_id, target_profile_id)
            if season_number is not None:
                await sonarr.search_season(http, settings, arr_id, season_number)
            else:
                await sonarr.search_series(http, settings, arr_id)
    except UpstreamError as exc:
        db.execute(_UPDATE_QUALITY_ERROR, (str(exc), req_id))
        return JSONResponse(
            {"state": "error", "error": f"{exc.service} unreachable"}, status_code=502
        )

    db.execute(_UPDATE_QUALITY_APPROVED, (now.isoformat(), req_id))
    await _notify_requester(db, settings, row, approved=True)
    return {"state": "approved"}


class QualityDenyBody(BaseModel):
    """Request body for ``POST /api/admin/quality/{req_id}/deny``."""

    note: str | None = Field(default=None, max_length=1000)


@router.post("/quality/{req_id}/deny", response_model=None)
async def post_quality_deny(
    req_id: int,
    request: Request,
    body: QualityDenyBody = QualityDenyBody(),
    user: dict = Depends(require_owner),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Deny a quality request, closing it out with no arr calls made.

    Only valid from ``pending_approval`` -- an ``error`` row must be
    retried (approve) or left alone, not denied, since a prior approve call
    already made upstream calls for it.

    Returns:
        200 ``{"state": "denied"}`` on success. 404 if the request doesn't
        exist. 409 ``{"error": ...}`` if not in ``pending_approval``.
    """
    settings: Settings = request.app.state.settings
    now = datetime.now(timezone.utc)
    row = db.execute("SELECT * FROM quality_requests WHERE id = ?", (req_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": f"no quality request with id {req_id}"}, status_code=404)
    if row["state"] != "pending_approval":
        return JSONResponse(
            {"error": f"quality request {req_id} is in state {row['state']!r}, not 'pending_approval'"},
            status_code=409,
        )

    db.execute(_UPDATE_QUALITY_DENIED, (body.note, now.isoformat(), req_id))
    _log_event(db, now, user["name"], "quality_denied", f"request {req_id}: {body.note or ''}")
    await _notify_requester(db, settings, row, approved=False, note=body.note)
    return {"state": "denied"}


async def _notify_discover_requester(
    db: sqlite3.Connection, settings: Settings, row: sqlite3.Row, *, approved: bool
) -> None:
    """Tell the friend how their 4K ask landed.

    Push only, no Discord fallback -- Discord is the owner's channel, and
    falling back there would tell the wrong person (see ``_notify_requester``).
    The declined wording names the consolation explicitly: something *is*
    being downloaded, just not at 4K, and "declined" on its own would read as
    "you are getting nothing".
    """
    title = "Request approved" if approved else "4K declined"
    body = row["title"] if approved else f"grabbing {row['title']} in 1080p instead"
    await push.send_to_user(
        db, settings, row["requested_by"],
        {"title": title, "body": body, "tab": "pipeline"},
    )


async def _file_discover_4k_row(
    http: CachedHTTP,
    db: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    *,
    quality: str,
) -> Any:
    """File a parked Discover request upstream, attributed to the friend.

    Both outcomes of the 4K gate come through here -- approve at ``"4K"``,
    deny at ``"1080p"`` -- because the only thing the owner's decision changes
    is which audited profile it lands in. The seasons are the ones stored when
    the friend asked, so what gets filed is the pick they actually made.

    Raises:
        discover.UserMappingError: If the friend has no Jellyseerr user.
        UpstreamError: On a Jellyseerr refusal or outage.
    """
    seasons = json.loads(row["seasons_json"]) if row["seasons_json"] else None
    user_id = await discover.jellyseerr_user_id(http, settings, row["requested_by"])
    created = await jellyseerr.create_request(
        http,
        settings,
        media_type=row["media_type"],
        tmdb_id=row["tmdb_id"],
        user_id=user_id,
        seasons=seasons,
        profile_id=discover.profile_for(settings, row["media_type"], quality),
    )
    # Same hint the member route writes: the Pipeline board enriches from the
    # arr libraries, which do not know about a request filed seconds ago.
    #
    # Upsert rather than INSERT OR REPLACE: the 4K queue stores no artwork, so
    # a replace would null out a poster the member route had already recorded
    # for this title and leave the tile as blank stone.
    db.execute(
        "INSERT INTO title_hints (media_type, tmdb_id, title) VALUES (?, ?, ?)"
        " ON CONFLICT(media_type, tmdb_id) DO UPDATE SET title = excluded.title",
        (row["media_type"], row["tmdb_id"], row["title"]),
    )
    return created


def _load_pending_4k(db: sqlite3.Connection, req_id: int) -> sqlite3.Row | JSONResponse:
    """The row, or the response explaining why it cannot be acted on."""
    row = db.execute("SELECT * FROM discover_4k_requests WHERE id = ?", (req_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": f"no 4K request with id {req_id}"}, status_code=404)
    if row["state"] != "pending":
        return JSONResponse(
            {"error": f"4K request {req_id} is in state {row['state']!r}, not 'pending'"},
            status_code=409,
        )
    return row


@router.post("/discover-4k/{req_id}/approve", response_model=None)
async def post_discover_4k_approve(
    req_id: int,
    request: Request,
    user: dict = Depends(require_owner),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Approve a friend's 4K request: file it upstream in the 4K lane.

    The row only settles if the filing actually landed. A Jellyseerr outage
    leaves it ``pending`` so this can simply be pressed again -- a row marked
    ``approved`` with nothing filed would read as done and strand the friend.
    The one exception is a duplicate (409): the request already exists
    upstream, so the work is done and retrying forever would be pointless.

    Returns:
        200 ``{"state": "approved", "request_id": int | None}``. 404 if no
        such request. 409 if it is not pending, or Jellyseerr's own refusal
        wording. 502 if the friend cannot be mapped or Jellyseerr is down --
        the row stays pending in both cases.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    now = datetime.now(timezone.utc)

    row = _load_pending_4k(db, req_id)
    if isinstance(row, JSONResponse):
        return row

    try:
        created = await _file_discover_4k_row(http, db, settings, row, quality="4K")
    except discover.UserMappingError:
        return JSONResponse(
            {"error": f"couldn't map {row['requested_by_name']}'s Plex account"},
            status_code=502,
        )
    except UpstreamError as exc:
        if exc.status == 409:
            note = exc.detail or "already requested in Jellyseerr"
            if db.execute(
                _UPDATE_DISCOVER_4K, ("approved", note, now.isoformat(), req_id)
            ).rowcount == 0:
                return JSONResponse({"error": _ALREADY_SETTLED}, status_code=409)
            _log_event(db, now, user["name"], "discover_4k_approved", f"request {req_id}: {note}")
            await _notify_discover_requester(db, settings, row, approved=True)
            return {"state": "approved", "request_id": None, "note": note}
        # Any other refusal is relayed as a 502, never with Jellyseerr's own
        # status: a 401 from an unhappy API key would otherwise reach the
        # browser as a 401 and log the OWNER out mid-approval.
        # The wording still comes from upstream (bounded, 4xx-only).
        detail = exc.detail if exc.status is not None and exc.status < 500 else None
        return JSONResponse(
            {"error": detail or f"{exc.service} unreachable"}, status_code=502
        )

    # Filed upstream but the row was settled elsewhere in the meantime: say so
    # rather than pushing a second, contradictory outcome at the friend.
    if db.execute(
        _UPDATE_DISCOVER_4K, ("approved", None, now.isoformat(), req_id)
    ).rowcount == 0:
        return JSONResponse({"error": _ALREADY_SETTLED}, status_code=409)
    _log_event(db, now, user["name"], "discover_4k_approved", f"request {req_id}: {row['title']}")
    await _notify_discover_requester(db, settings, row, approved=True)
    return {
        "state": "approved",
        "request_id": created.get("id") if isinstance(created, dict) else None,
    }


class Discover4kDenyBody(BaseModel):
    """Request body for ``POST /api/admin/discover-4k/{req_id}/deny``."""

    note: str | None = Field(default=None, max_length=1000)


@router.post("/discover-4k/{req_id}/deny", response_model=None)
async def post_discover_4k_deny(
    req_id: int,
    request: Request,
    body: Discover4kDenyBody = Discover4kDenyBody(),
    user: dict = Depends(require_owner),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Decline the 4K ask -- and file the title at 1080p anyway.

    Denying is a decision about *size*, not about whether the friend gets to
    watch the thing. Dropping the request entirely would leave them waiting
    on a download that is never coming, which is the failure this whole queue
    exists to avoid, so the standard-quality request goes in as part of the
    denial. A duplicate (409) still settles the row -- the 1080p copy is
    already on its way -- but *every other* upstream refusal leaves it
    pending, because nothing was filed and a settled row would both vanish
    from the queue and tell the friend a download is coming.

    Returns:
        200 ``{"state": "denied", "request_id": int | None}``. 404 if no such
        request. 409 if it is not pending, or if another session settled it
        first. 502 if the friend cannot be mapped or Jellyseerr refused/was
        down -- the row stays pending in all of those.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    now = datetime.now(timezone.utc)

    row = _load_pending_4k(db, req_id)
    if isinstance(row, JSONResponse):
        return row

    request_id: int | None = None
    note = body.note
    try:
        created = await _file_discover_4k_row(http, db, settings, row, quality="1080p")
        request_id = created.get("id") if isinstance(created, dict) else None
    except discover.UserMappingError:
        return JSONResponse(
            {"error": f"couldn't map {row['requested_by_name']}'s Plex account"},
            status_code=502,
        )
    except UpstreamError as exc:
        if exc.status != 409:
            # Nothing was filed. Settling the row here would drop it out of
            # the queue *and* push "grabbing it in 1080p instead" at a friend
            # who is getting nothing -- exactly the stranding this queue
            # exists to prevent. So it stays pending and the deny can simply
            # be pressed again, matching the approve route. The status is
            # clamped to 502 for the same reason it is there: a 401 from an
            # unhappy API key would log the *owner* out mid-approval.
            detail = exc.detail if exc.status is not None and exc.status < 500 else None
            return JSONResponse(
                {"error": detail or f"{exc.service} unreachable"}, status_code=502
            )
        # Jellyseerr already has the 1080p copy, so the work the denial owed
        # the friend is done and the row can settle -- with the upstream's
        # wording kept on the note so the owner can see what happened.
        upstream = exc.detail or "already requested in Jellyseerr"
        note = f"{note} — {upstream}" if note else upstream

    if db.execute(
        _UPDATE_DISCOVER_4K, ("denied", note, now.isoformat(), req_id)
    ).rowcount == 0:
        return JSONResponse({"error": _ALREADY_SETTLED}, status_code=409)
    _log_event(db, now, user["name"], "discover_4k_denied", f"request {req_id}: {note or ''}")
    await _notify_discover_requester(db, settings, row, approved=False)
    return {"state": "denied", "request_id": request_id}


@router.post("/access-requests/{req_id}/approve", response_model=None)
async def post_access_approve(
    req_id: int,
    request: Request,
    user: dict = Depends(require_owner),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Approve an access request: share the libraries on plex.tv, add the member.

    plex.tv is called *before* anything is written, and a failure leaves the
    row exactly as it was -- ``pending``, with no ``users`` row. That order
    matters: the DB saying "approved" while the share never happened would
    read as done in the queue and strand someone who still can't sign in.
    Retrying is just pressing the button again.

    Returns:
        200 ``{"state": "approved"}`` on success. 404 if no such request.
        409 ``{"error": ...}`` if it isn't ``pending``. 502 ``{"error":
        "plex.tv unreachable"}`` -- sanitized, no upstream detail -- if the
        share call fails, with the row left retryable.
    """
    settings: Settings = request.app.state.settings
    http: CachedHTTP = request.app.state.http
    now = datetime.now(timezone.utc)

    row = db.execute("SELECT * FROM access_requests WHERE id = ?", (req_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": f"no access request with id {req_id}"}, status_code=404)
    if row["state"] != "pending":
        return JSONResponse(
            {"error": f"access request {req_id} is in state {row['state']!r}, not 'pending'"},
            status_code=409,
        )

    try:
        await plex_tv.invite_to_server(http, settings, email=row["email"])
    except UpstreamError as exc:
        return JSONResponse({"error": f"{exc.service} unreachable"}, status_code=502)

    db.execute(_UPDATE_ACCESS_APPROVED, (now.isoformat(), req_id))
    db.execute(
        _UPSERT_APPROVED_MEMBER,
        (row["plex_account_id"], row["name"], now.isoformat()),
    )
    _log_event(
        db, now, user["name"], "access_approved", f"{row['name']} <{row['email']}>"
    )
    return {"state": "approved"}


class AccessDenyBody(BaseModel):
    """Request body for ``POST /api/admin/access-requests/{req_id}/deny``."""

    note: str | None = Field(default=None, max_length=1000)


@router.post("/access-requests/{req_id}/deny", response_model=None)
async def post_access_deny(
    req_id: int,
    body: AccessDenyBody = AccessDenyBody(),
    user: dict = Depends(require_owner),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Decline an access request. Nothing is shared and nobody is invited.

    A denied row is terminal: the guest route answers 409 to any further
    press of the button, so this is also what stops someone re-asking.

    Returns:
        200 ``{"state": "denied"}`` on success. 404 if no such request. 409
        ``{"error": ...}`` if it isn't ``pending``.
    """
    now = datetime.now(timezone.utc)

    row = db.execute("SELECT * FROM access_requests WHERE id = ?", (req_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": f"no access request with id {req_id}"}, status_code=404)
    if row["state"] != "pending":
        return JSONResponse(
            {"error": f"access request {req_id} is in state {row['state']!r}, not 'pending'"},
            status_code=409,
        )

    db.execute(_UPDATE_ACCESS_DENIED, (body.note, now.isoformat(), req_id))
    _log_event(db, now, user["name"], "access_denied", f"{row['name']}: {body.note or ''}")
    return {"state": "denied"}


_LIST_USERS = """
SELECT plex_account_id, name, role, last_seen, revoked FROM users
ORDER BY name COLLATE NOCASE
"""


@router.get("/users", response_model=None)
async def get_admin_users(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """List every account that has ever logged in, with its revocation state.

    Returns:
        ``{"users": [{"id", "name", "role", "last_seen", "revoked"}, ...]}``.
    """
    rows = db.execute(_LIST_USERS).fetchall()
    return {
        "users": [
            {
                "id": r["plex_account_id"],
                "name": r["name"],
                "role": r["role"],
                "last_seen": r["last_seen"],
                "revoked": r["revoked"],
            }
            for r in rows
        ]
    }


def _set_revoked(
    db: sqlite3.Connection, plex_account_id: int, revoked: int, user: dict, now: datetime,
) -> dict[str, Any] | JSONResponse:
    """Shared body of the revoke/unrevoke routes."""
    row = db.execute(
        "SELECT plex_account_id, name FROM users WHERE plex_account_id = ?",
        (plex_account_id,),
    ).fetchone()
    if row is None:
        return JSONResponse({"error": f"no user with id {plex_account_id}"}, status_code=404)

    # Revoking yourself would 401 your own next request, leaving nobody able
    # to reach this route and un-revoke it -- a lockout with no in-app way out.
    if revoked and plex_account_id == user["id"]:
        return JSONResponse({"error": "you cannot revoke your own access"}, status_code=409)

    db.execute(
        "UPDATE users SET revoked = ? WHERE plex_account_id = ?", (revoked, plex_account_id)
    )
    _log_event(
        db, now, user["name"], "user_revoked" if revoked else "user_unrevoked",
        f"{row['name']} ({plex_account_id})",
    )
    return {"id": plex_account_id, "revoked": revoked}


@router.post("/users/{plex_account_id}/revoke", response_model=None)
async def post_user_revoke(
    plex_account_id: int,
    user: dict = Depends(require_owner),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Revoke an account: its existing session 401s on the next request.

    Returns:
        200 ``{"id": ..., "revoked": 1}``. 404 if no such user. 409 if the
        owner tries to revoke themselves.
    """
    return _set_revoked(db, plex_account_id, 1, user, datetime.now(timezone.utc))


@router.post("/users/{plex_account_id}/unrevoke", response_model=None)
async def post_user_unrevoke(
    plex_account_id: int,
    user: dict = Depends(require_owner),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    """Restore a revoked account's access.

    Returns:
        200 ``{"id": ..., "revoked": 0}``. 404 if no such user.
    """
    return _set_revoked(db, plex_account_id, 0, user, datetime.now(timezone.utc))
