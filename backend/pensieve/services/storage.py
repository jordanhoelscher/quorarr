"""Storage summary: disk usage plus Radarr/Sonarr library breakdown.

Powers the member-facing storage view. Disk numbers come straight from
``os.statvfs`` on the configured media mount; the movie/series byte totals
come from the already-cached Radarr and Sonarr library listings.
"""

import os
from typing import Any

from starlette.concurrency import run_in_threadpool

from pensieve.clients import radarr, sonarr
from pensieve.clients.base import CachedHTTP
from pensieve.config import Settings


async def summary(http: CachedHTTP, settings: Settings) -> dict[str, Any]:
    """Build the storage summary: disk usage + per-library byte/item counts.

    Args:
        http: Shared cached HTTP client.
        settings: App settings, for ``media_mount`` and the arr client URLs/keys.

    Returns:
        Dict with ``total_bytes``, ``used_bytes``, ``free_bytes``,
        ``movies_bytes``, ``tv_bytes``, ``movie_count``, ``series_count``.

    Raises:
        UpstreamError: If Radarr or Sonarr is unreachable (caller may fall
            back to stale cached data).
    """
    # In a worker thread, never on the event loop: ``media_mount`` is a hard
    # NFS mount, and a hung statvfs there would block every other route --
    # including /health, which turns a dead NAS into a container restart loop.
    vfs = await run_in_threadpool(os.statvfs, settings.media_mount)
    total_bytes = vfs.f_frsize * vfs.f_blocks
    free_bytes = vfs.f_frsize * vfs.f_bavail
    used_bytes = total_bytes - free_bytes

    movies = await radarr.list_movies(http, settings)
    series = await sonarr.list_series(http, settings)

    return {
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "movies_bytes": sum(m["size_bytes"] for m in movies),
        "tv_bytes": sum(s["size_bytes"] for s in series),
        "movie_count": len(movies),
        "series_count": len(series),
    }
