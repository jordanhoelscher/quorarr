"""Radarr v3 API client: movie library, download queue, and profile/search actions.

Every call goes through ``CachedHTTP`` with ``service="radarr"``. GETs that back
list/browse views use a TTL cache; the raw-movie fetch used ahead of a PUT
round-trip (``get_movie``, and internally by ``set_profile``) always uses
``ttl=0`` so the mutate-and-PUT never operates on a stale copy.
"""

from typing import Any

from pensieve.clients.base import CachedHTTP
from pensieve.config import Settings


def _base(settings: Settings) -> str:
    """Build the Radarr v3 API base URL from settings."""
    return f"{settings.radarr_url}/api/v3"


def _headers(settings: Settings) -> dict[str, str]:
    """Build the standard headers Radarr requires on every request."""
    return {"X-Api-Key": settings.radarr_api_key}


def shape_movies(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape raw Radarr movie objects into the frontend's movie dict.

    Split out from ``list_movies`` so a stale (cached-but-unshaped) response
    fetched via ``CachedHTTP.stale`` can be shaped the same way a live one is.

    Args:
        raw: Raw Radarr movie objects, as returned by ``GET /api/v3/movie``.

    Returns:
        One dict per movie: ``arr_id``, ``title``, ``year``, ``tmdb_id``,
        ``size_bytes``, ``quality``, ``resolution``, ``profile_id``,
        ``poster``, ``added``, ``has_file``. ``quality``/``resolution`` are
        ``None`` when the movie has no file on disk.
    """
    quality = (
        lambda m: m.get("movieFile", {}).get("quality", {}).get("quality", {})
    )
    return [
        {
            "arr_id": m["id"],
            "title": m["title"],
            "year": m["year"],
            "tmdb_id": m["tmdbId"],
            "size_bytes": m["sizeOnDisk"],
            "quality": quality(m).get("name"),
            "resolution": quality(m).get("resolution"),
            "profile_id": m["qualityProfileId"],
            # .get on both keys: one malformed image record would otherwise
            # KeyError out of the whole shaping pass, 500-ing the library view
            # (and, for movies, /api/storage) over a single missing poster URL.
            "poster": next(
                (i.get("remoteUrl") for i in m.get("images", [])
                 if i.get("coverType") == "poster"),
                None,
            ),
            "added": m.get("added"),
            "has_file": m["hasFile"],
        }
        for m in raw
    ]


async def list_movies(http: CachedHTTP, settings: Settings, ttl: float = 600) -> list[dict[str, Any]]:
    """List all movies in the Radarr library, shaped for the frontend.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``radarr_url``/``radarr_api_key``.
        ttl: Cache freshness window in seconds.

    Returns:
        See ``shape_movies``.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    movies = await http.get_json(
        f"{_base(settings)}/movie", service="radarr", headers=_headers(settings), ttl=ttl
    )
    return shape_movies(movies)


def shape_queue(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Shape a raw Radarr queue body into the frontend's queue-record dicts.

    Split out from ``get_queue`` so a stale (cached-but-unshaped) response
    fetched via ``CachedHTTP.stale`` can be shaped the same way a live one is.

    Args:
        raw: Raw Radarr queue body, as returned by ``GET /api/v3/queue``.

    Returns:
        One dict per queue record: ``tmdb_id``, ``title``, ``size``,
        ``sizeleft``, ``timeleft``, ``status``, ``pct`` (0 when ``size`` is 0,
        never raises ``ZeroDivisionError``).
    """
    return [
        {
            "tmdb_id": rec.get("movie", {}).get("tmdbId"),
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
    """List active Radarr download queue records, shaped for the frontend.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``radarr_url``/``radarr_api_key``.
        ttl: Cache freshness window in seconds.

    Returns:
        See ``shape_queue``.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    body = await http.get_json(
        f"{_base(settings)}/queue",
        service="radarr",
        headers=_headers(settings),
        params={"pageSize": 100, "includeUnknownMovieItems": "false"},
        ttl=ttl,
    )
    return shape_queue(body)


async def get_movie(http: CachedHTTP, settings: Settings, arr_id: int) -> dict[str, Any]:
    """Fetch a single movie's raw Radarr representation, uncached.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``radarr_url``/``radarr_api_key``.
        arr_id: Radarr movie id.

    Returns:
        The raw Radarr movie object, needed verbatim for a PUT round-trip.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    return await http.get_json(
        f"{_base(settings)}/movie/{arr_id}", service="radarr", headers=_headers(settings), ttl=0
    )


async def set_profile(http: CachedHTTP, settings: Settings, arr_id: int, profile_id: int) -> None:
    """Switch a movie's quality profile via a GET-then-PUT round-trip.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``radarr_url``/``radarr_api_key``.
        arr_id: Radarr movie id.
        profile_id: New ``qualityProfileId`` to set.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    movie = await get_movie(http, settings, arr_id)
    movie["qualityProfileId"] = profile_id
    await http.send_json(
        "PUT",
        f"{_base(settings)}/movie/{arr_id}",
        service="radarr",
        headers=_headers(settings),
        json=movie,
    )


async def search_movie(http: CachedHTTP, settings: Settings, arr_id: int) -> None:
    """Trigger a Radarr search for a movie.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``radarr_url``/``radarr_api_key``.
        arr_id: Radarr movie id.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    await http.send_json(
        "POST",
        f"{_base(settings)}/command",
        service="radarr",
        headers=_headers(settings),
        json={"name": "MoviesSearch", "movieIds": [arr_id]},
    )


async def delete_movie(http: CachedHTTP, settings: Settings, arr_id: int) -> None:
    """Delete a movie from Radarr, including its files on disk.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``radarr_url``/``radarr_api_key``.
        arr_id: Radarr movie id.

    Raises:
        UpstreamError: On a connect/transport error or non-2xx response.
    """
    await http.send_json(
        "DELETE",
        f"{_base(settings)}/movie/{arr_id}",
        service="radarr",
        headers=_headers(settings),
        params={"deleteFiles": "true", "addImportExclusion": "false"},
    )
