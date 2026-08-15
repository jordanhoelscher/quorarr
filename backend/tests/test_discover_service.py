"""Tests for the discover service: Plex->Jellyseerr user mapping, shelves, season guard."""
import json
from pathlib import Path

import httpx
import pytest

from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.services import discover
from tests.conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures"
TRENDING = json.loads((FIXTURES / "jellyseerr_discover_trending.json").read_text())
USERS = json.loads((FIXTURES / "jellyseerr_users.json").read_text())


def make_http(handler):
    return CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# --- user mapping ------------------------------------------------------------


async def test_user_id_matches_an_existing_plex_account(tmp_path):
    s = make_settings(tmp_path)
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=USERS)

    assert await discover.jellyseerr_user_id(make_http(handler), s, 222222) == 4
    assert seen == [("GET", "/api/v1/user")]


async def test_user_id_imports_from_plex_on_a_miss(tmp_path):
    """A friend who has never used Jellyseerr still gets their own attribution."""
    s = make_settings(tmp_path)
    seen = []
    after_import = {
        "results": [*USERS["results"], {"id": 9, "plexId": 333333, "displayName": "Ash"}]
    }

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            assert json.loads(request.content) == {"plexIds": ["333333"]}
            return httpx.Response(201, json=[{"id": 9, "plexId": 333333}])
        # The list is only re-served post-import because the helper refetches
        # with ttl=0; a cached read here would still be missing the new row.
        return httpx.Response(200, json=after_import if ("POST", "/api/v1/user/import-from-plex") in seen else USERS)

    assert await discover.jellyseerr_user_id(make_http(handler), s, 333333) == 9
    assert seen == [
        ("GET", "/api/v1/user"),
        ("POST", "/api/v1/user/import-from-plex"),
        ("GET", "/api/v1/user"),
    ]


async def test_user_id_fails_loud_when_the_import_does_not_help(tmp_path):
    """Never fall back to the API key's owner -- a wrong name is worse than an error."""
    s = make_settings(tmp_path)

    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json=[])
        return httpx.Response(200, json=USERS)

    with pytest.raises(discover.UserMappingError):
        await discover.jellyseerr_user_id(make_http(handler), s, 999999)


async def test_user_id_fails_loud_when_the_import_itself_errors(tmp_path):
    """An unimportable account is a mapping failure, not a Jellyseerr outage."""
    s = make_settings(tmp_path)

    def handler(request):
        if request.method == "POST":
            return httpx.Response(500)
        return httpx.Response(200, json=USERS)

    with pytest.raises(discover.UserMappingError):
        await discover.jellyseerr_user_id(make_http(handler), s, 999999)


async def test_user_id_propagates_a_dead_jellyseerr(tmp_path):
    """"Jellyseerr is down" and "I can't find you" are different answers."""
    s = make_settings(tmp_path)

    def handler(request):
        return httpx.Response(502)

    with pytest.raises(UpstreamError):
        await discover.jellyseerr_user_id(make_http(handler), s, 222222)


# --- shelves -----------------------------------------------------------------


async def test_shelves_returns_three_named_shelves(tmp_path):
    s = make_settings(tmp_path)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=TRENDING)

    shelves = await discover.shelves(make_http(handler), s)
    assert [shelf["id"] for shelf in shelves] == ["trending", "popular", "upcoming"]
    assert all(shelf["title"] for shelf in shelves)
    assert all(len(shelf["items"]) == 3 for shelf in shelves)
    assert all(shelf["error"] is None for shelf in shelves)
    assert sorted(seen) == [
        "/api/v1/discover/movies",
        "/api/v1/discover/movies/upcoming",
        "/api/v1/discover/trending",
    ]


async def test_shelves_marks_one_failed_shelf_rather_than_faking_an_empty_one(tmp_path):
    s = make_settings(tmp_path)

    def handler(request):
        if request.url.path.endswith("/upcoming"):
            return httpx.Response(500)
        return httpx.Response(200, json=TRENDING)

    shelves = await discover.shelves(make_http(handler), s)
    by_id = {shelf["id"]: shelf for shelf in shelves}
    assert by_id["trending"]["error"] is None
    assert by_id["upcoming"]["items"] == []
    assert by_id["upcoming"]["error"] == "jellyseerr unreachable"


async def test_shelves_raises_when_every_shelf_fails(tmp_path):
    """Three error boxes is not a page -- let the route answer 502 once."""
    s = make_settings(tmp_path)

    def handler(request):
        return httpx.Response(500)

    with pytest.raises(UpstreamError):
        await discover.shelves(make_http(handler), s)


# --- season guard ------------------------------------------------------------


_DETAIL = {
    "media_type": "tv",
    "seasons": [
        {"season_number": 1, "requestable": False, "availability": "available"},
        {"season_number": 2, "requestable": False, "availability": "requested"},
        {"season_number": 3, "requestable": True, "availability": "requestable"},
        {"season_number": 4, "requestable": True, "availability": "requestable"},
    ],
}


def test_requestable_seasons():
    assert discover.requestable_seasons(_DETAIL) == {3, 4}


def test_requestable_seasons_of_a_movie_is_empty():
    assert discover.requestable_seasons({"media_type": "movie", "seasons": None}) == set()


@pytest.mark.parametrize("wanted", [[3], [3, 4], [4]])
def test_check_seasons_accepts_a_subset(wanted):
    assert discover.check_seasons(_DETAIL, wanted) is None


@pytest.mark.parametrize("wanted", [[1], [2], [3, 1], [99]])
def test_check_seasons_rejects_anything_already_had_or_asked_for(wanted):
    message = discover.check_seasons(_DETAIL, wanted)
    assert message and "season" in message.lower()


def test_check_seasons_rejects_an_empty_pick():
    assert discover.check_seasons(_DETAIL, []) is not None


# --- request profiles ---------------------------------------------------------


def test_profile_for_movie_is_always_the_radarr_hd_lane(tmp_path):
    """Movies carry an explicit profile too -- never Jellyseerr's default."""
    settings = make_settings(tmp_path)
    assert discover.profile_for(settings, "movie", "1080p") == settings.radarr_profile_hd_id


def test_profile_for_tv_1080p_is_the_sonarr_hd_lane(tmp_path):
    settings = make_settings(tmp_path)
    assert discover.profile_for(settings, "tv", "1080p") == settings.sonarr_profile_hd_id


def test_profile_for_tv_720p_is_the_sonarr_720_lane(tmp_path):
    settings = make_settings(tmp_path)
    assert discover.profile_for(settings, "tv", "720p") == settings.sonarr_profile_720_id


def test_profile_for_tv_720p_is_none_when_the_lane_is_unconfigured(tmp_path):
    """Unset means unset: better a loud 502 than a silent fall to 1080p."""
    settings = make_settings(tmp_path, sonarr_profile_720_id=0)
    assert discover.profile_for(settings, "tv", "720p") is None


def test_profile_for_owner_4k_lanes(tmp_path):
    """4K is reachable, but only ever through the owner-gated quality value."""
    settings = make_settings(tmp_path)
    assert discover.profile_for(settings, "movie", "4K") == settings.radarr_profile_4k_id
    assert discover.profile_for(settings, "tv", "4K") == settings.sonarr_profile_4k_id


def test_profile_for_never_returns_a_4k_lane_for_hd_qualities(tmp_path):
    """The friend-facing vocabulary is HD; nothing there may resolve to 4K."""
    settings = make_settings(tmp_path)
    banned = {settings.radarr_profile_4k_id, settings.sonarr_profile_4k_id}
    picked = {
        discover.profile_for(settings, media_type, quality)
        for media_type in ("movie", "tv")
        for quality in ("1080p", "720p")
    }
    assert not (picked & banned)
