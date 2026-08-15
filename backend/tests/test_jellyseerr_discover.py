"""Tests for the v0.4.0 Jellyseerr discover/search/request client surface.

Fixtures are trimmed captures of a live Jellyseerr 2.x (2026-08-13), so the
field names here are the real ones (``posterPath``, ``mediaInfo.status``,
``mediaInfo.seasons[].status``), not a guess at the schema.
"""
import json
from pathlib import Path

import httpx
import pytest

from pensieve.clients import jellyseerr
from pensieve.clients.base import CachedHTTP, UpstreamError
from tests.conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures"
TRENDING = json.loads((FIXTURES / "jellyseerr_discover_trending.json").read_text())
SEARCH = json.loads((FIXTURES / "jellyseerr_search.json").read_text())
MOVIE_DETAIL = json.loads((FIXTURES / "jellyseerr_movie_detail.json").read_text())
TV_DETAIL = json.loads((FIXTURES / "jellyseerr_tv_detail.json").read_text())
USERS = json.loads((FIXTURES / "jellyseerr_users.json").read_text())


def make_http(handler):
    return CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# --- shaping -----------------------------------------------------------------


def test_shape_media_movie():
    item = SEARCH["results"][0]
    assert jellyseerr.shape_media(item) == {
        "tmdb_id": 603,
        "title": "The Matrix",
        "year": 1999,
        "media_type": "movie",
        "poster_path": "/dXNAPwY7VrqMAo51EKhhCJfaGb5.jpg",
        "overview": item["overview"],
        "rating": 8.255,
        "status": 5,
        "availability": "available",
    }


def test_shape_media_tv_uses_name_and_first_air_date():
    shaped = jellyseerr.shape_media(SEARCH["results"][1])
    assert shaped["media_type"] == "tv"
    assert shaped["title"] == "Threat Matrix"
    assert shaped["year"] == 2003
    # Unknown to Jellyseerr -> no mediaInfo at all -> free to ask for.
    assert shaped["status"] is None
    assert shaped["availability"] == "requestable"


def test_shape_media_drops_people():
    """A person row has no tmdbId to request and must never reach the UI."""
    assert jellyseerr.shape_media(SEARCH["results"][2]) is None


def test_shape_media_drops_rows_without_an_id():
    assert jellyseerr.shape_media({"mediaType": "movie", "title": "Nameless"}) is None


def test_shape_media_missing_optionals():
    shaped = jellyseerr.shape_media({"id": 7, "mediaType": "movie"})
    assert shaped == {
        "tmdb_id": 7,
        "title": "",
        "year": None,
        "media_type": "movie",
        "poster_path": None,
        "overview": "",
        "rating": None,
        "status": None,
        "availability": "requestable",
    }


def test_shape_media_blank_release_date_is_not_a_year():
    assert jellyseerr.shape_media({"id": 7, "mediaType": "movie", "releaseDate": ""})["year"] is None


@pytest.mark.parametrize(
    ("status", "availability"),
    [
        (None, "requestable"),
        (1, "requestable"),
        (2, "requested"),
        (3, "requested"),
        (4, "partial"),
        (5, "available"),
        # Jellyseerr 2.x statuses past 5 (deleted/blacklisted) are not
        # "on the server", so they stay askable -- Jellyseerr itself is the
        # authority and will 409 if it disagrees.
        (7, "requestable"),
    ],
)
def test_availability_of(status, availability):
    assert jellyseerr.availability_of(status) == availability


def test_shape_results_drops_people_and_keeps_order():
    shaped = jellyseerr.shape_results(SEARCH)
    assert [row["tmdb_id"] for row in shaped] == [603, 711]


def test_shape_results_tolerates_a_missing_results_key():
    assert jellyseerr.shape_results({}) == []


# --- search ------------------------------------------------------------------


async def test_search_calls_endpoint_and_shapes(tmp_path):
    s = make_settings(tmp_path)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.path == "/api/v1/search"
        assert request.url.params["query"] == "matrix"
        assert request.url.params["page"] == "1"
        assert request.headers["X-Api-Key"] == "jk"
        return httpx.Response(200, json=SEARCH)

    rows = await jellyseerr.search(make_http(handler), s, "matrix")
    assert len(seen) == 1
    assert [row["title"] for row in rows] == ["The Matrix", "Threat Matrix"]


async def test_search_propagates_upstream_failure(tmp_path):
    s = make_settings(tmp_path)

    def handler(request):
        return httpx.Response(500)

    with pytest.raises(UpstreamError) as exc:
        await jellyseerr.search(make_http(handler), s, "matrix")
    assert exc.value.service == "jellyseerr"


# --- discover ----------------------------------------------------------------


async def test_discover_trending(tmp_path):
    s = make_settings(tmp_path)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=TRENDING)

    rows = await jellyseerr.discover_trending(make_http(handler), s)
    assert seen == ["/api/v1/discover/trending"]
    assert len(rows) == 3
    assert rows[0]["media_type"] == "tv"


async def test_discover_movies_popular_and_upcoming_hit_distinct_paths(tmp_path):
    s = make_settings(tmp_path)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=TRENDING)

    http = make_http(handler)
    await jellyseerr.discover_movies_popular(http, s)
    await jellyseerr.discover_movies_upcoming(http, s)
    assert seen == ["/api/v1/discover/movies", "/api/v1/discover/movies/upcoming"]


async def test_discover_ttl_is_reused_within_the_window(tmp_path):
    """The 15-minute shelf TTL means a second reader costs no upstream call."""
    s = make_settings(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=TRENDING)

    http = make_http(handler)
    await jellyseerr.discover_trending(http, s, ttl=900)
    await jellyseerr.discover_trending(http, s, ttl=900)
    assert len(calls) == 1


# --- detail ------------------------------------------------------------------


async def test_media_detail_movie(tmp_path):
    s = make_settings(tmp_path)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=MOVIE_DETAIL)

    detail = await jellyseerr.media_detail(make_http(handler), s, "movie", 550)
    assert seen == ["/api/v1/movie/550"]
    assert detail["tmdb_id"] == 550
    assert detail["title"] == "Fight Club"
    assert detail["year"] == 1999
    assert detail["media_type"] == "movie"
    assert detail["availability"] == "available"
    assert detail["runtime"] == 139
    # Movies carry no season list at all, rather than an empty one.
    assert detail["seasons"] is None


async def test_media_detail_tv_marks_season_availability(tmp_path):
    s = make_settings(tmp_path)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=TV_DETAIL)

    detail = await jellyseerr.media_detail(make_http(handler), s, "tv", 1399)
    assert seen == ["/api/v1/tv/1399"]
    assert detail["media_type"] == "tv"
    assert detail["title"] == "Game of Thrones"

    # Specials (season 0) are never offered: nobody means the specials when
    # they ask for a show, and Sonarr does not monitor them by default.
    assert [s_["season_number"] for s_ in detail["seasons"]] == [1, 2, 3, 4]

    by_number = {s_["season_number"]: s_ for s_ in detail["seasons"]}
    assert by_number[1]["availability"] == "available"
    assert by_number[1]["requestable"] is False
    assert by_number[2]["availability"] == "requested"
    assert by_number[2]["requestable"] is False
    assert by_number[3]["availability"] == "requestable"
    assert by_number[3]["requestable"] is True
    assert by_number[3]["episode_count"] == 10
    assert by_number[3]["name"] == "Season 3"


async def test_media_detail_rejects_an_unknown_media_type(tmp_path):
    s = make_settings(tmp_path)

    def handler(request):  # pragma: no cover -- must never be reached
        raise AssertionError("no request should be made")

    with pytest.raises(ValueError):
        await jellyseerr.media_detail(make_http(handler), s, "person", 1)


# --- users -------------------------------------------------------------------


async def test_list_users_shapes_plex_ids(tmp_path):
    s = make_settings(tmp_path)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.path == "/api/v1/user"
        assert request.url.params["take"] == "100"
        return httpx.Response(200, json=USERS)

    users = await jellyseerr.list_users(make_http(handler), s)
    assert users == [
        {"id": 1, "plex_id": 111111},
        {"id": 4, "plex_id": 222222},
    ]


async def test_list_users_skips_rows_without_a_plex_id(tmp_path):
    """A local (non-Plex) Jellyseerr account can never match a the app session."""
    s = make_settings(tmp_path)

    def handler(request):
        return httpx.Response(
            200, json={"results": [{"id": 9, "plexId": None}, {"id": 10, "plexId": 5}]}
        )

    assert await jellyseerr.list_users(make_http(handler), s) == [{"id": 10, "plex_id": 5}]


async def test_import_plex_users_posts_string_ids(tmp_path):
    s = make_settings(tmp_path)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v1/user/import-from-plex"
        assert json.loads(request.content) == {"plexIds": ["222222"]}
        return httpx.Response(201, json=[{"id": 4, "plexId": 222222}])

    created = await jellyseerr.import_plex_users(make_http(handler), s, [222222])
    assert len(seen) == 1
    assert created == [{"id": 4, "plexId": 222222}]


# --- request -----------------------------------------------------------------


async def test_create_request_movie_body_has_no_4k_field(tmp_path):
    s = make_settings(tmp_path)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        assert request.method == "POST"
        assert request.url.path == "/api/v1/request"
        return httpx.Response(201, json={"id": 88, "status": 1})

    result = await jellyseerr.create_request(
        make_http(handler), s, media_type="movie", tmdb_id=550, user_id=4
    )
    assert sent == [{"mediaType": "movie", "mediaId": 550, "userId": 4}]
    assert "is4k" not in sent[0]
    assert result == {"id": 88, "status": 1}


async def test_create_request_tv_sends_seasons(tmp_path):
    s = make_settings(tmp_path)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 89})

    await jellyseerr.create_request(
        make_http(handler), s, media_type="tv", tmdb_id=1399, user_id=4, seasons=[3, 4]
    )
    assert sent == [{"mediaType": "tv", "mediaId": 1399, "userId": 4, "seasons": [3, 4]}]


async def test_create_request_surfaces_jellyseerrs_own_message(tmp_path):
    """A duplicate is Jellyseerr's call to make; its wording has to survive."""
    s = make_settings(tmp_path)

    def handler(request):
        return httpx.Response(409, json={"message": "Request for this media already exists"})

    with pytest.raises(UpstreamError) as exc:
        await jellyseerr.create_request(
            make_http(handler), s, media_type="movie", tmdb_id=550, user_id=4
        )
    assert exc.value.status == 409
    assert exc.value.detail == "Request for this media already exists"


async def test_create_request_sends_the_profile_id_when_given(tmp_path):
    s = make_settings(tmp_path)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 90})

    await jellyseerr.create_request(
        make_http(handler), s, media_type="tv", tmdb_id=1399, user_id=4,
        seasons=[2], profile_id=3,
    )
    assert sent == [
        {"mediaType": "tv", "mediaId": 1399, "userId": 4, "seasons": [2], "profileId": 3}
    ]
    # profileId picks the *quality profile*; is4k is a different switch and
    # stays the owner's manual valve regardless of which profile is filed against.
    assert "is4k" not in sent[0]


async def test_create_request_omits_profile_id_when_not_given(tmp_path):
    """None means 'send no key' -- never profileId: null, which Jellyseerr 400s."""
    s = make_settings(tmp_path)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 91})

    await jellyseerr.create_request(
        make_http(handler), s, media_type="movie", tmdb_id=550, user_id=4
    )
    assert "profileId" not in sent[0]
