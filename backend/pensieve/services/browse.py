"""Filter state -> Jellyseerr discover parameters. Pure, and deliberately so.

Every rule about what the app asks upstream is decided here: which path,
which sort key, which date-bound parameter names, and which floors. Keeping
it I/O-free means the whole rule set is pinned by fast unit tests, and
keeping it in *one* function means the movie/TV parameter-name split cannot
drift across call sites -- that split is the likeliest source of a silently
ignored filter.

Two upstream behaviours shape this module and are worth stating plainly:

* Jellyseerr answers HTTP 200 for an unknown ``sortBy`` and quietly returns
  the default ordering. So ``sort`` is a closed vocabulary here and the
  string that reaches the wire is always one this module chose.
* ``vote_average.desc`` with no vote-count floor surfaces titles with a
  single 10/10 vote. Any rating constraint therefore drags a floor with it.
"""

from datetime import date
from typing import Any, Literal

Sort = Literal["trending", "popular", "newest", "upcoming", "top_rated"]
Media = Literal["movie", "tv"]
Decade = Literal["2020s", "2010s", "2000s", "1990s", "older"]

#: Minimum vote count before a rating constraint is trustworthy. TV runs an
#: order of magnitude below film, so a shared floor would either admit junk
#: films or hide most legitimate shows.
_VOTE_FLOOR: dict[str, int] = {"movie": 300, "tv": 100}

#: Per-media date-bound parameter names and the sort keys that use them.
_DATE_FIELD: dict[str, str] = {"movie": "primaryReleaseDate", "tv": "firstAirDate"}
_DATE_SORT: dict[str, str] = {"movie": "release_date", "tv": "first_air_date"}

_DECADES: dict[str, tuple[str | None, str]] = {
    "2020s": ("2020-01-01", "2029-12-31"),
    "2010s": ("2010-01-01", "2019-12-31"),
    "2000s": ("2000-01-01", "2009-12-31"),
    "1990s": ("1990-01-01", "1999-12-31"),
    "older": (None, "1989-12-31"),
}


class BrowseFilterConflict(Exception):
    """Raised when filters are combined with a sort that cannot honour them.

    Only ``trending`` can raise this: it is a distinct upstream endpoint that
    takes no narrowing parameters at all. Refusing is the point -- silently
    dropping the filters would hand back a page that looks filtered and
    isn't, which is precisely the failure mode this module exists to avoid.
    """


def to_upstream(
    *,
    sort: Sort,
    media: Media | None,
    genre: int | None,
    decade: Decade | None,
    min_rating: int | None,
    page: int,
    today: date,
) -> tuple[str, dict[str, Any]]:
    """Translate browse filters into a Jellyseerr path and query parameters.

    Args:
        sort: The chosen ordering. A closed vocabulary; never a client string.
        media: ``"movie"``, ``"tv"``, or None for "not supplied" (defaults to
            movie for every sort except ``trending``, which forbids it).
        genre: TMDB genre id, already scoped to ``media`` by the caller.
        decade: Coarse release-window filter.
        min_rating: Rating floor (7 or 8); always paired with a vote floor.
        page: 1-based page number.
        today: Injected rather than read from the clock, so "newest" and
            "upcoming" are testable and reproducible.

    Returns:
        ``(path, params)`` ready to hand to ``jellyseerr.browse_page``.

    Raises:
        BrowseFilterConflict: If ``sort`` is ``trending`` and any filter was
            supplied.
    """
    if sort == "trending":
        if media or genre is not None or decade is not None or min_rating is not None:
            raise BrowseFilterConflict("trending cannot be narrowed by filters")
        return "/discover/trending", {"page": page}

    kind: str = media or "movie"
    path = "/discover/movies" if kind == "movie" else "/discover/tv"
    params: dict[str, Any] = {"page": page}

    # Lower/upper date bounds accumulate from two independent sources (the
    # sort and the decade) and are intersected, so the tighter one always
    # wins rather than the later one overwriting the earlier.
    gte: str | None = None
    lte: str | None = None

    if sort == "popular":
        params["sortBy"] = "popularity.desc"
    elif sort == "newest":
        params["sortBy"] = f"{_DATE_SORT[kind]}.desc"
        lte = today.isoformat()
    elif sort == "upcoming":
        params["sortBy"] = f"{_DATE_SORT[kind]}.asc"
        gte = today.isoformat()
    elif sort == "top_rated":
        params["sortBy"] = "vote_average.desc"
        params["voteCountGte"] = _VOTE_FLOOR[kind]

    if genre is not None:
        params["genre"] = genre

    if decade is not None:
        decade_gte, decade_lte = _DECADES[decade]
        gte = max(filter(None, (gte, decade_gte)), default=None)
        lte = min(filter(None, (lte, decade_lte)), default=None)

    if min_rating is not None:
        params["voteAverageGte"] = min_rating
        params["voteCountGte"] = _VOTE_FLOOR[kind]

    field = _DATE_FIELD[kind]
    if gte is not None:
        params[f"{field}Gte"] = gte
    if lte is not None:
        params[f"{field}Lte"] = lte

    return path, params
