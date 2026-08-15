"""Library browse service: movie/series listing shaping and season quality mix.

Pure functions consuming already-shaped Radarr/Sonarr client output (see
``pensieve.clients.radarr``/``pensieve.clients.sonarr``). Movie/series rows
drop ``profile_id`` (an internal detail used by quality-request actions, not
the browse view) and gain a ``media_type`` discriminator. Season-level quality
mix is only computed at detail level (``series_detail``), since it requires
an extra per-series episode-file fetch that the list views don't need.
"""

from collections import Counter
from typing import Any


def movies(radarr_movies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape Radarr movie rows for the library browse view.

    Args:
        radarr_movies: Output of ``radarr.list_movies``/``radarr.shape_movies``.

    Returns:
        Same rows minus ``profile_id``, plus ``"media_type": "movie"``.
    """
    return [_drop_profile_id(m) | {"media_type": "movie"} for m in radarr_movies]


def series_list(sonarr_series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape Sonarr series rows for the library browse view.

    Args:
        sonarr_series: Output of ``sonarr.list_series``/``sonarr.shape_series``.

    Returns:
        Same rows minus ``profile_id``, plus ``"media_type": "series"``. Each
        row's ``seasons`` list is passed through untouched -- no quality mix
        at this level, since that requires an extra per-series fetch that
        only ``series_detail`` performs.
    """
    return [_drop_profile_id(s) | {"media_type": "series"} for s in sonarr_series]


def series_detail(series_row: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any]:
    """Build one series' detail view: rollup row plus per-season quality mix.

    Args:
        series_row: One row as shaped by ``sonarr.list_series``/``shape_series``
            (still carries ``profile_id`` -- this function drops it).
        files: Episode files for this series, as shaped by ``sonarr.episode_files``.

    Returns:
        A ``series_list``-shaped row (``media_type: series``, no ``profile_id``)
        where each season dict gains a ``"qualities"`` count dict, e.g.
        ``{"Bluray-1080p": 12, "HDTV-720p": 3}``, built from ``files`` filtered
        to that season. A file with ``quality: None`` counts under the key
        ``"unknown"``. Does not mutate ``series_row`` or its seasons.
    """
    shaped = _drop_profile_id(series_row) | {"media_type": "series"}
    shaped["seasons"] = [
        {**season, "qualities": _quality_mix(files, season["season_number"])}
        for season in series_row["seasons"]
    ]
    return shaped


def _quality_mix(files: list[dict[str, Any]], season_number: int) -> dict[str, int]:
    """Count episode files by quality label for a single season number."""
    counts = Counter(
        f["quality"] or "unknown" for f in files if f["season_number"] == season_number
    )
    return dict(counts)


def _drop_profile_id(row: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``row`` without the ``profile_id`` key."""
    return {k: v for k, v in row.items() if k != "profile_id"}
