import json
from pathlib import Path

import httpx

from pensieve.clients.base import CachedHTTP
from pensieve.clients import radarr
from tests.conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures"
MOVIES = json.loads((FIXTURES / "radarr_movie.json").read_text())
QUEUE = json.loads((FIXTURES / "radarr_queue.json").read_text())


def make_http(handler):
    return CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_list_movies_shapes_fixture(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.url.path == "/api/v3/movie"
        assert request.headers["X-Api-Key"] == "rk"
        return httpx.Response(200, json=MOVIES)

    http = make_http(handler)
    movies = await radarr.list_movies(http, s)
    assert len(captured) == 1
    assert len(movies) == 3

    arrival = movies[0]
    assert arrival == {
        "arr_id": 101,
        "title": "Arrival",
        "year": 2016,
        "tmdb_id": 329865,
        "size_bytes": 8589934592,
        "quality": "Bluray-1080p",
        "resolution": 1080,
        "profile_id": 6,
        "poster": "https://image.tmdb.org/t/p/original/poster-arrival.jpg",
        "added": "2024-01-15T12:00:00Z",
        "has_file": True,
    }

    # Movie with no file at all (no movieFile key) -> quality/resolution None.
    oppenheimer = movies[2]
    assert oppenheimer["has_file"] is False
    assert oppenheimer["quality"] is None
    assert oppenheimer["resolution"] is None
    assert oppenheimer["poster"] == "https://image.tmdb.org/t/p/original/poster-oppenheimer.jpg"


async def test_list_movies_poster_none_when_no_poster_image(tmp_path):
    s = make_settings(tmp_path)

    def handler(request):
        return httpx.Response(200, json=[
            {
                "id": 999,
                "title": "No Poster",
                "year": 2020,
                "tmdbId": 1,
                "sizeOnDisk": 0,
                "qualityProfileId": 6,
                "hasFile": False,
                "added": "2024-01-01T00:00:00Z",
                "images": [{"coverType": "fanart", "remoteUrl": "https://example.com/fanart.jpg"}],
            }
        ])

    http = make_http(handler)
    movies = await radarr.list_movies(http, s)
    assert movies[0]["poster"] is None


async def test_get_queue_pct_math_including_zero_size(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.url.path == "/api/v3/queue"
        assert request.url.params["pageSize"] == "100"
        assert request.url.params["includeUnknownMovieItems"] == "false"
        return httpx.Response(200, json=QUEUE)

    http = make_http(handler)
    queue = await radarr.get_queue(http, s)
    assert len(captured) == 1
    assert queue[0] == {
        "tmdb_id": 438631,
        "title": "Dune.2021.2160p.WEB-DL",
        "size": 21474836480,
        "sizeleft": 5368709120,
        "timeleft": "01:30:00",
        "status": "downloading",
        "pct": 75,
    }
    # size=0 must not raise ZeroDivisionError and must report pct=0.
    assert queue[1] == {
        "tmdb_id": 872585,
        "title": "Oppenheimer.2023.1080p.Bluray",
        "size": 0,
        "sizeleft": 0,
        "timeleft": "00:00:00",
        "status": "queued",
        "pct": 0,
    }


async def test_get_movie_raw(tmp_path):
    s = make_settings(tmp_path)

    def handler(request):
        assert request.url.path == "/api/v3/movie/101"
        assert request.method == "GET"
        return httpx.Response(200, json=MOVIES[0])

    http = make_http(handler)
    movie = await radarr.get_movie(http, s, 101)
    assert movie == MOVIES[0]


async def test_set_profile_get_then_put(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        if request.method == "GET":
            assert request.url.path == "/api/v3/movie/101"
            return httpx.Response(200, json=MOVIES[0])
        assert request.method == "PUT"
        assert request.url.path == "/api/v3/movie/101"
        body = json.loads(request.content)
        assert body["qualityProfileId"] == 7
        # rest of the movie object must round-trip untouched.
        assert body["title"] == "Arrival"
        assert body["id"] == 101
        return httpx.Response(200, json=body)

    http = make_http(handler)
    result = await radarr.set_profile(http, s, 101, 7)
    assert result is None
    assert len(captured) == 2
    assert captured[0].method == "GET"
    assert captured[1].method == "PUT"


async def test_search_movie_posts_command(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v3/command"
        body = json.loads(request.content)
        assert body == {"name": "MoviesSearch", "movieIds": [101]}
        return httpx.Response(200, json={"id": 1})

    http = make_http(handler)
    result = await radarr.search_movie(http, s, 101)
    assert result is None
    assert len(captured) == 1


async def test_delete_movie_sends_params(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.method == "DELETE"
        assert request.url.path == "/api/v3/movie/101"
        assert request.url.params["deleteFiles"] == "true"
        assert request.url.params["addImportExclusion"] == "false"
        return httpx.Response(200)

    http = make_http(handler)
    result = await radarr.delete_movie(http, s, 101)
    assert result is None
    assert len(captured) == 1


def test_shape_movies_survives_a_poster_image_without_remote_url(tmp_path):
    """One malformed image record must not 500 both /library/movies and /storage."""
    shaped = radarr.shape_movies(
        [
            {
                "id": 999, "title": "Broken Poster", "year": 2020, "tmdbId": 1,
                "sizeOnDisk": 10, "qualityProfileId": 6, "hasFile": True,
                "images": [{"coverType": "poster"}, {"coverType": "fanart", "remoteUrl": "f"}],
            }
        ]
    )
    assert shaped[0]["poster"] is None
