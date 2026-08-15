"""Sonarr v3 API client: series library, download queue, and profile/search/season actions.

Every call goes through ``CachedHTTP`` with ``service="sonarr"``. GETs that back
list/browse views use a TTL cache; the raw-series fetch used ahead of a PUT
round-trip (``get_series``, and internally by ``set_profile`` and
``delete_season``) always uses ``ttl=0`` so the mutate-and-PUT never operates
on a stale copy.
"""

from typing import Any

from pensieve.clients.base import CachedHTTP
from pensieve.config import Settings


def _base(settings: Settings) -> str:
    """Build the Sonarr v3 API base URL from settings."""
    return f"{settings.sonarr_url}/api/v3"


def _headers(settings: Settings) -> dict[str, str]:
    """Build the standard headers Sonarr requires on every request."""
    return {"X-Api-Key": settings.sonarr_api_key}


def shape_series(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape raw Sonarr series objects into the frontend's series dict.

    Split out from ``list_series`` so a stale (cached-but-unshaped) response
    fetched via ``CachedHTTP.stale`` can be shaped the same way a live one is.

    Args:
        raw: Raw Sonarr series objects, as returned by ``GET /api/v3/series``.

    Returns:
        One dict per series: ``arr_id``, ``title``, ``year``, ``tvdb_id``,
        ``size_bytes``, ``episode_count``, ``profile_id``, ``poster``,
        ``added``, ``seasons``. ``seasons`` drops season 0 (specials) entries
        that have no files on disk, since those clutter the UI with nothing
        to act on; a specials season that does have files is kept.
    """
    return [
        {
            "arr_id": s["id"],
            "title": s["title"],
            "year": s["year"],
            "tvdb_id": s["tvdbId"],
            "size_bytes": s["statistics"]["sizeOnDisk"],
            "episode_count": s["statistics"]["episodeFileCount"],
            "profile_id": s["qualityProfileId"],
            # .get on both keys: one malformed image record would otherwise
            # KeyError out of the whole shaping pass, 500-ing the library view
            # (and, for movies, /api/storage) over a single missing poster URL.
            "poster": next(
                (i.get("remoteUrl") for i in s.get("images", [])
                 if i.get("coverType") == "poster"),
                None,
            ),
            "added": s.get("added"),
            "seasons": [
                {
                    "season_number": season["seasonNumber"],
                    "size_bytes": season["statistics"]["sizeOnDisk"],
                    "episode_file_count": season["statistics"]["episodeFileCount"],
                    "monitored": season["monitored"],
                }
                for season in s["seasons"]
                if season["seasonNumber"] != 0 or season["statistics"]["episodeFileCount"] > 0
            ],
        }
        for s in raw
    ]


async def list_series(http: CachedHTTP, settings: Settings, ttl: float = 600) -> list[dict[str, Any]]:
    """List all series in the Sonarr library, shaped for the frontend.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``sonarr_url``/``sonarr_api_key``.
        ttl: Cache freshness window in seconds.

    Returns:
        See ``shape_series``.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    series = await http.get_json(
        f"{_base(settings)}/series", service="sonarr", headers=_headers(settings), ttl=ttl
    )
    return shape_series(series)


async def episode_files(
    http: CachedHTTP, settings: Settings, series_id: int, ttl: float = 600
) -> list[dict[str, Any]]:
    """List episode files for a series, shaped for the frontend.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``sonarr_url``/``sonarr_api_key``.
        series_id: Sonarr series id to fetch files for.
        ttl: Cache freshness window in seconds.

    Returns:
        One dict per episode file: ``id``, ``season_number``, ``size_bytes``,
        ``quality``, ``resolution``.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    files = await http.get_json(
        f"{_base(settings)}/episodefile",
        service="sonarr",
        headers=_headers(settings),
        params={"seriesId": series_id},
        ttl=ttl,
    )
    return [
        {
            "id": f["id"],
            "season_number": f["seasonNumber"],
            "size_bytes": f["size"],
            "quality": f["quality"]["quality"]["name"],
            "resolution": f["quality"]["quality"]["resolution"],
        }
        for f in files
    ]


def shape_queue(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Shape a raw Sonarr queue body into the frontend's queue-record dicts.

    Split out from ``get_queue`` so a stale (cached-but-unshaped) response
    fetched via ``CachedHTTP.stale`` can be shaped the same way a live one is.

    Args:
        raw: Raw Sonarr queue body, as returned by ``GET /api/v3/queue``.

    Returns:
        One dict per queue record: ``tvdb_id``, ``title``, ``size``,
        ``sizeleft``, ``timeleft``, ``status``, ``pct`` (0 when ``size`` is 0,
        never raises ``ZeroDivisionError``).
    """
    return [
        {
            "tvdb_id": rec.get("series", {}).get("tvdbId"),
            "title": rec["title"],
            "size": rec["size"],
            "sizeleft": rec["sizeleft"],
            "timeleft": rec.get("timeleft"),
            "status": rec["status"],
            "pct": round((1 - rec["sizeleft"] / rec["size"]) * 100) if rec["size"] else 0,
        }
        for rec in raw["records"]
    ]


async def get_queue(http: CachedHTTP, settings: Settings, ttl: float = 30) -> list[dict[str, Any]]:
    """List active Sonarr download queue records, shaped for the frontend.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``sonarr_url``/``sonarr_api_key``.
        ttl: Cache freshness window in seconds.

    Returns:
        See ``shape_queue``.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    body = await http.get_json(
        f"{_base(settings)}/queue",
        service="sonarr",
        headers=_headers(settings),
        params={"pageSize": 100},
        ttl=ttl,
    )
    return shape_queue(body)


async def get_series(http: CachedHTTP, settings: Settings, arr_id: int) -> dict[str, Any]:
    """Fetch a single series' raw Sonarr representation, uncached.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``sonarr_url``/``sonarr_api_key``.
        arr_id: Sonarr series id.

    Returns:
        The raw Sonarr series object, needed verbatim for a PUT round-trip.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    return await http.get_json(
        f"{_base(settings)}/series/{arr_id}", service="sonarr", headers=_headers(settings), ttl=0
    )


async def set_profile(http: CachedHTTP, settings: Settings, arr_id: int, profile_id: int) -> None:
    """Switch a series' quality profile via a GET-then-PUT round-trip.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``sonarr_url``/``sonarr_api_key``.
        arr_id: Sonarr series id.
        profile_id: New ``qualityProfileId`` to set.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    series = await get_series(http, settings, arr_id)
    series["qualityProfileId"] = profile_id
    await http.send_json(
        "PUT",
        f"{_base(settings)}/series/{arr_id}",
        service="sonarr",
        headers=_headers(settings),
        json=series,
    )


async def search_series(http: CachedHTTP, settings: Settings, arr_id: int) -> None:
    """Trigger a Sonarr search for an entire series.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``sonarr_url``/``sonarr_api_key``.
        arr_id: Sonarr series id.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    await http.send_json(
        "POST",
        f"{_base(settings)}/command",
        service="sonarr",
        headers=_headers(settings),
        json={"name": "SeriesSearch", "seriesId": arr_id},
    )


async def search_season(http: CachedHTTP, settings: Settings, arr_id: int, season: int) -> None:
    """Trigger a Sonarr search for a single season of a series.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``sonarr_url``/``sonarr_api_key``.
        arr_id: Sonarr series id.
        season: Season number to search.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    await http.send_json(
        "POST",
        f"{_base(settings)}/command",
        service="sonarr",
        headers=_headers(settings),
        json={"name": "SeasonSearch", "seriesId": arr_id, "seasonNumber": season},
    )


async def delete_series(http: CachedHTTP, settings: Settings, arr_id: int) -> None:
    """Delete a series from Sonarr, including its files on disk.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``sonarr_url``/``sonarr_api_key``.
        arr_id: Sonarr series id.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    await http.send_json(
        "DELETE",
        f"{_base(settings)}/series/{arr_id}",
        service="sonarr",
        headers=_headers(settings),
        params={"deleteFiles": "true"},
    )


async def delete_season(http: CachedHTTP, settings: Settings, arr_id: int, season: int) -> int:
    """Delete a single season's files and unmonitor it, leaving the series intact.

    Two steps: (1) delete every episode file belonging to ``season``, so the
    disk space is reclaimed immediately; (2) flip that season's ``monitored``
    flag to ``False`` via a GET-then-PUT of the raw series object, so Sonarr
    doesn't immediately re-grab what was just deleted. Every other season's
    ``monitored`` state is round-tripped untouched.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``sonarr_url``/``sonarr_api_key``.
        arr_id: Sonarr series id.
        season: Season number to delete.

    Returns:
        The number of episode files that were deleted.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    files = await episode_files(http, settings, arr_id, ttl=0)
    to_delete = [f for f in files if f["season_number"] == season]
    for f in to_delete:
        await http.send_json(
            "DELETE",
            f"{_base(settings)}/episodefile/{f['id']}",
            service="sonarr",
            headers=_headers(settings),
        )

    series = await get_series(http, settings, arr_id)
    for season_entry in series["seasons"]:
        if season_entry["seasonNumber"] == season:
            season_entry["monitored"] = False
    await http.send_json(
        "PUT",
        f"{_base(settings)}/series/{arr_id}",
        service="sonarr",
        headers=_headers(settings),
        json=series,
    )

    return len(to_delete)
