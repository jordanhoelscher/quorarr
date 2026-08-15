"""Unified pipeline board: a pure join of Jellyseerr requests against arr queues.

``build()`` has no I/O or upstream dependency -- it takes already-shaped
request/queue lists (see ``pensieve.clients.jellyseerr``/``radarr``/``sonarr``)
and produces one "card" per request, joined against whichever download queue
matches its media type. This keeps the join logic unit-testable without any
HTTP mocking; the member route (``GET /api/pipeline``) is a thin I/O wrapper
around it.
"""

from datetime import datetime, timedelta
from typing import Any

from pensieve.clients.jellyseerr import STATUS_LABELS

# Radarr/Sonarr queue "status" values that mean an active transfer -- these
# drive the aggregate card status to "downloading" and contribute to its
# pct/timeleft/count.
_DOWNLOADING_STATUSES = {"downloading", "queued"}

# Queue "status" values that indicate a problem worth flagging, without
# overriding the request's own jellyseerr stage.
_WARNING_STATUSES = {"warning", "failed", "stalled"}

_AVAILABLE_STATUS = 5
_STALE_AVAILABLE_AGE = timedelta(days=14)


def _parse_timeleft(value: str | None) -> int | None:
    """Parse a Radarr/Sonarr ``timeleft`` string into whole seconds.

    Accepts plain ``"HH:MM:SS"`` and the day-prefixed ``"D.HH:MM:SS"`` form
    both arrs emit once the remaining time exceeds 24 hours. String
    comparison of these values is unsafe (``"1.02:00:00"`` sorts before
    ``"23:00:00"`` alphabetically despite being longer), so callers must
    compare on the parsed integer instead.

    Args:
        value: Raw ``timeleft`` string, or ``None``.

    Returns:
        Total seconds, or ``None`` if ``value`` is missing or malformed.
    """
    if not value:
        return None

    days = 0
    rest = value
    if "." in value:
        day_part, _, rest = value.partition(".")
        try:
            days = int(day_part)
        except ValueError:
            return None

    parts = rest.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(p) for p in parts)
    except ValueError:
        return None

    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _max_timeleft(values: list[str | None]) -> str | None:
    """Return whichever raw ``timeleft`` string represents the longest duration.

    Args:
        values: Raw ``timeleft`` strings (possibly ``None``) from matched
            queue rows.

    Returns:
        The original string with the largest parsed duration. Unparseable or
        missing values are skipped in favor of any parseable one; if none
        parse, returns ``None``.
    """
    best_value: str | None = None
    best_seconds = -1
    for value in values:
        seconds = _parse_timeleft(value)
        if seconds is None:
            continue
        if seconds > best_seconds:
            best_seconds = seconds
            best_value = value
    return best_value


def _is_stale_available(request: dict[str, Any], now: datetime) -> bool:
    """True if ``request`` is an "available" request older than 14 days.

    Args:
        request: A shaped jellyseerr request dict.
        now: Reference time for the age comparison.

    Returns:
        Whether the request should be dropped from the board.
    """
    if request.get("status") != _AVAILABLE_STATUS:
        return False

    created_at = request.get("created_at")
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False

    if created.tzinfo is None:
        created = created.replace(tzinfo=now.tzinfo)

    return (now - created) > _STALE_AVAILABLE_AGE


def _build_card(request: dict[str, Any], matched: list[dict[str, Any]]) -> dict[str, Any]:
    """Build one pipeline card from a request and its matched queue rows.

    Args:
        request: A shaped jellyseerr request dict.
        matched: Queue rows (radarr or sonarr, matching this request's media
            type) whose id matched this request's ``tmdb_id``/``tvdb_id``.

    Returns:
        A card dict: ``title``, ``media_type``, ``requested_by``,
        ``created_at``, ``status``, ``pct``, ``timeleft``, ``warning``,
        ``count``. ``title`` prefers a matched queue row's title, then the
        request's own ``title`` (populated by ``enrich_titles`` from the
        library caches), and is ``None`` only when neither exists. ``status`` falls back to ``"unknown"`` for a jellyseerr
        status code not present in ``STATUS_LABELS`` (e.g. a future code
        this deploy doesn't know about yet), rather than ``None``.
    """
    base_status = STATUS_LABELS.get(request.get("status"), "unknown")
    downloading_rows = [r for r in matched if r.get("status") in _DOWNLOADING_STATUSES]
    warning_rows = [r for r in matched if r.get("status") in _WARNING_STATUSES]

    title = request.get("title")
    status = base_status
    pct = None
    timeleft = None
    warning = None
    count = None

    if downloading_rows:
        status = "downloading"
        title = downloading_rows[0].get("title") or title
        # "size"/"pct" can be present-but-None (not just absent) on a queue
        # row, so `.get(key, 0)` alone doesn't guard it -- `or 0` does.
        total_size = sum(r.get("size") or 0 for r in downloading_rows)
        if total_size:
            pct = round(
                sum((r.get("pct") or 0) * (r.get("size") or 0) for r in downloading_rows) / total_size
            )
        else:
            pct = 0
        timeleft = _max_timeleft([r.get("timeleft") for r in downloading_rows])
        count = len(downloading_rows)
    elif warning_rows:
        title = warning_rows[0].get("title") or title
        warning = warning_rows[0].get("status")

    return {
        "title": title,
        "media_type": request.get("media_type"),
        "requested_by": request.get("requested_by"),
        "created_at": request.get("created_at"),
        "status": status,
        "pct": pct,
        "timeleft": timeleft,
        "warning": warning,
        "count": count,
    }


def enrich_titles(
    requests: list[dict[str, Any]],
    *,
    movie_titles: dict[Any, str],
    series_titles: dict[Any, str],
    hint_titles: dict[tuple[Any, Any], str] | None = None,
) -> list[dict[str, Any]]:
    """Return copies of ``requests`` with ``title`` filled from library maps.

    Jellyseerr's request API carries no media title, so without this every
    card whose media isn't currently in a download queue renders a generic
    fallback. Titles come from the (cached) Radarr/Sonarr libraries keyed by
    ``tmdb_id``/``tvdb_id``. Pure function; the input dicts are not mutated.

    Args:
        requests: Shaped jellyseerr requests.
        movie_titles: ``tmdb_id`` -> title from the Radarr library.
        series_titles: ``tvdb_id`` -> title from the Sonarr library.

    Returns:
        New request dicts; ``title`` is set where a map has the id, left
        absent/None otherwise.
    """
    hint_titles = hint_titles or {}
    enriched = []
    for request in requests:
        request = dict(request)
        if request.get("title") is None:
            if request.get("media_type") == "movie":
                request["title"] = movie_titles.get(request.get("tmdb_id"))
            elif request.get("media_type") == "tv":
                request["title"] = series_titles.get(request.get("tvdb_id"))
            if request.get("title") is None:
                request["title"] = hint_titles.get(
                    (request.get("media_type"), request.get("tmdb_id"))
                )
        enriched.append(request)
    return enriched


def build(
    requests: list[dict[str, Any]],
    radarr_q: list[dict[str, Any]],
    sonarr_q: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Join shaped Jellyseerr requests against Radarr/Sonarr download queues.

    Movie requests match ``radarr_q`` on ``tmdb_id``; TV requests match
    ``sonarr_q`` on ``tvdb_id``. A queue row with a ``None`` id is never
    indexed, so it can never match; a request with a ``None`` id likewise
    never matches. Pure function -- no I/O, no upstream dependency.

    Args:
        requests: Shaped jellyseerr requests (see
            ``pensieve.clients.jellyseerr.shape_requests``).
        radarr_q: Shaped Radarr queue rows (see
            ``pensieve.clients.radarr.shape_queue``).
        sonarr_q: Shaped Sonarr queue rows (see
            ``pensieve.clients.sonarr.shape_queue``).
        now: Reference time for dropping stale "available" requests.

    Returns:
        One card per request (see ``_build_card``), excluding "available"
        requests older than 14 days.
    """
    radarr_by_tmdb: dict[Any, list[dict[str, Any]]] = {}
    for rec in radarr_q:
        tmdb_id = rec.get("tmdb_id")
        if tmdb_id is not None:
            radarr_by_tmdb.setdefault(tmdb_id, []).append(rec)

    sonarr_by_tvdb: dict[Any, list[dict[str, Any]]] = {}
    for rec in sonarr_q:
        tvdb_id = rec.get("tvdb_id")
        if tvdb_id is not None:
            sonarr_by_tvdb.setdefault(tvdb_id, []).append(rec)

    cards = []
    for request in requests:
        if _is_stale_available(request, now):
            continue

        media_type = request.get("media_type")
        matched: list[dict[str, Any]] = []
        if media_type == "movie" and request.get("tmdb_id") is not None:
            matched = radarr_by_tmdb.get(request["tmdb_id"], [])
        elif media_type == "tv" and request.get("tvdb_id") is not None:
            matched = sonarr_by_tvdb.get(request["tvdb_id"], [])

        cards.append(_build_card(request, matched))

    return cards
