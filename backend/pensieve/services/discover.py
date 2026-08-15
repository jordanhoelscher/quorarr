"""Discover-tab logic: browse shelves, Plex->Jellyseerr attribution, season guard.

The one genuinely delicate piece here is ``jellyseerr_user_id``. This app
knows a friend by their Plex account id; Jellyseerr wants its own user id on
``POST /request``. Get that wrong and the request still succeeds -- filed
under whoever owns the API key -- so the failure is silent, wears
the wrong name forever, and eats the wrong person's quota. Hence the
fail-loud contract: match, else import from Plex and match again, else raise.
There is deliberately no "attribute it to the admin" fallback.
"""

import asyncio
from typing import Any

from pensieve.clients import jellyseerr
from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.config import Settings

#: The three shelves, in the order the tab renders them.
_SHELVES: tuple[tuple[str, str, Any], ...] = (
    ("trending", "Trending", jellyseerr.discover_trending),
    ("popular", "Popular films", jellyseerr.discover_movies_popular),
    ("upcoming", "Coming soon", jellyseerr.discover_movies_upcoming),
)

#: How long a cached Jellyseerr user list stays fresh. Long enough that the
#: common case (an established friend) costs nothing per request; short
#: enough that a user added directly in Jellyseerr is picked up without a
#: restart. A miss refetches at ttl=0 anyway, so staleness can only ever cost
#: an extra round trip, never a wrong answer.
_USER_TTL = 300


class UserMappingError(Exception):
    """Raised when a Plex account cannot be resolved to a Jellyseerr user.

    Carries no upstream detail on purpose: the route turns this into one
    fixed, human sentence, and the reason is always the same shape ("we
    could not find or create your Jellyseerr account").
    """


def _match(users: list[dict[str, Any]], plex_account_id: int) -> int | None:
    """The Jellyseerr user id for a Plex account id, or None if absent."""
    return next((u["id"] for u in users if u["plex_id"] == plex_account_id), None)


async def jellyseerr_user_id(
    http: CachedHTTP, settings: Settings, plex_account_id: int
) -> int:
    """Resolve a session's Plex account id to a Jellyseerr user id.

    Tries the cached user list first, then imports the account from Plex's
    share list and re-reads the list live (the cached copy predates the
    import, so it must not be trusted for the second look).

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        plex_account_id: The session user's Plex account id.

    Returns:
        The Jellyseerr user id to attribute requests to.

    Raises:
        UserMappingError: If the account is still unmatched after an import
            attempt -- including when the import call itself fails, since
            from the caller's side that is the same outcome.
        UpstreamError: If Jellyseerr cannot be reached to answer at all.
            Kept distinct from ``UserMappingError`` so "Jellyseerr is down"
            never reads as "you don't have an account".
    """
    users = await jellyseerr.list_users(http, settings, ttl=_USER_TTL)
    matched = _match(users, plex_account_id)
    if matched is not None:
        return matched

    try:
        await jellyseerr.import_plex_users(http, settings, [plex_account_id])
    except UpstreamError as exc:
        raise UserMappingError(
            f"plex account {plex_account_id} could not be imported into jellyseerr"
        ) from exc

    users = await jellyseerr.list_users(http, settings, ttl=0)
    matched = _match(users, plex_account_id)
    if matched is None:
        raise UserMappingError(
            f"plex account {plex_account_id} has no jellyseerr user after import"
        )
    return matched


async def shelves(http: CachedHTTP, settings: Settings, ttl: float = 900) -> list[dict[str, Any]]:
    """The three browse shelves, fetched concurrently.

    A shelf that fails comes back empty with its ``error`` set rather than
    silently short -- an empty shelf and a broken one must not look alike.
    If *every* shelf fails, the error is raised instead so the route can
    answer 502 once, rather than rendering a page made entirely of error
    boxes.

    Args:
        http: Shared cached HTTP client.
        settings: App settings.
        ttl: Cache freshness window in seconds for each shelf.

    Returns:
        ``[{"id", "title", "items": [...], "error": str | None}, ...]``.

    Raises:
        UpstreamError: If no shelf could be fetched at all.
    """
    results = await asyncio.gather(
        *(fetch(http, settings, ttl=ttl) for _id, _title, fetch in _SHELVES),
        return_exceptions=True,
    )

    built: list[dict[str, Any]] = []
    failure: BaseException | None = None
    for (shelf_id, title, _fetch), result in zip(_SHELVES, results):
        if isinstance(result, BaseException):
            failure = result
            service = getattr(result, "service", "jellyseerr")
            built.append(
                {"id": shelf_id, "title": title, "items": [], "error": f"{service} unreachable"}
            )
        else:
            built.append({"id": shelf_id, "title": title, "items": result, "error": None})

    if failure is not None and all(shelf["error"] for shelf in built):
        raise failure
    return built


def profile_for(settings: Settings, media_type: str, quality: str) -> int | None:
    """The arr quality profile a Discover request should be filed against.

    Every request carries an explicit ``profileId``, including movies: leaving
    it off means inheriting whatever default the Jellyseerr/arr pairing
    happens to have that day, which is exactly the kind of unaudited lane the
    house profile contract exists to close. The vocabulary here is the whole
    vocabulary -- HD-1080p, HD-720p, and the owner-only 4K lanes -- so there
    is no path from this function to an un-audited profile.

    Args:
        settings: App settings, holding the four audited profile ids plus the
            optional 720p lane.
        media_type: ``"movie"`` or ``"tv"``.
        quality: ``"1080p"``, ``"720p"`` (TV only), or ``"4K"`` (owner only;
            the *caller* enforces that, not this function).

    Returns:
        The profile id, or None when 720p was asked for on a deploy that has
        no ``SONARR_PROFILE_720_ID``. None is never "use the default" -- the
        route turns it into a 502.
    """
    if media_type == "movie":
        return (
            settings.radarr_profile_4k_id if quality == "4K"
            else settings.radarr_profile_hd_id
        )
    if quality == "4K":
        return settings.sonarr_profile_4k_id
    if quality == "720p":
        return settings.sonarr_profile_720_id or None
    return settings.sonarr_profile_hd_id


def requestable_seasons(detail: dict[str, Any]) -> set[int]:
    """Season numbers that are neither on the server nor already requested."""
    return {
        season["season_number"]
        for season in detail.get("seasons") or []
        if season["requestable"]
    }


def check_seasons(detail: dict[str, Any], wanted: list[int]) -> str | None:
    """Validate a TV season pick against what is actually still askable.

    The picker disables unavailable seasons, but the picker is client-side:
    the request body is whatever the caller chose to send. Re-checking here
    is what stops a crafted (or simply stale) body from re-requesting a
    season the server already has.

    Args:
        detail: A ``jellyseerr.media_detail`` result for the TV title.
        wanted: Season numbers from the request body.

    Returns:
        None if the pick is valid, otherwise a friend-readable refusal.
    """
    if not wanted:
        return "Pick at least one season."

    available = requestable_seasons(detail)
    extra = sorted(set(wanted) - available)
    if extra:
        listed = ", ".join(str(number) for number in extra)
        subject = f"Season {listed} is" if len(extra) == 1 else f"Seasons {listed} are"
        return f"{subject} already on the server or already requested."
    return None
