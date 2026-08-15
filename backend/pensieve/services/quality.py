"""Quality tier logic for media profile decisions."""

from pensieve.config import Settings


RESOLUTION = {"1080p": 1080, "4K": 2160}


def decide_tier(requested: str) -> str:
    """Decide approval tier based on requested resolution.

    Args:
        requested: Resolution string ("1080p" or "4K").

    Returns:
        "auto" for 1080p, "approval" for 4K.

    Raises:
        ValueError: If resolution is not supported.
    """
    if requested == "1080p":
        return "auto"
    elif requested == "4K":
        return "approval"
    else:
        raise ValueError(f"Unsupported resolution: {requested}")


def target_profile_id(*, media_type: str, requested: str, settings: Settings) -> int:
    """The profile id a request for ``requested`` should land the item on.

    Split out of ``plan_action`` so the owner-approval path can ask the same
    question without inventing a ``current_profile_id`` it doesn't have.

    Args:
        media_type: "movie" or "series".
        requested: Requested resolution ("1080p" or "4K").
        settings: Settings object with profile IDs.

    Returns:
        The Radarr/Sonarr quality profile id to switch to.

    Raises:
        ValueError: If media_type is not supported.
    """
    if media_type == "movie":
        hd_id, _4k_id = settings.radarr_profile_hd_id, settings.radarr_profile_4k_id
    elif media_type == "series":
        hd_id, _4k_id = settings.sonarr_profile_hd_id, settings.sonarr_profile_4k_id
    else:
        raise ValueError(f"Unsupported media_type: {media_type}")

    return _4k_id if requested == "4K" else hd_id


def plan_action(
    *,
    media_type: str,
    requested: str,
    current_profile_id: int,
    settings: Settings,
) -> dict:
    """Plan profile action for a media item.

    Args:
        media_type: "movie" or "series".
        requested: Requested resolution ("1080p" or "4K").
        current_profile_id: Current Radarr/Sonarr profile ID.
        settings: Settings object with profile IDs.

    Returns:
        Dict with tier, target_profile_id, and needs_profile_switch.
    """
    tier = decide_tier(requested)
    target = target_profile_id(
        media_type=media_type, requested=requested, settings=settings
    )

    return {
        "tier": tier,
        "target_profile_id": target,
        "needs_profile_switch": current_profile_id != target,
    }
