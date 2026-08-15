import json
from pathlib import Path

import httpx

from pensieve.clients.base import CachedHTTP
from pensieve.clients import sonarr
from tests.conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures"
SERIES = json.loads((FIXTURES / "sonarr_series.json").read_text())
EPISODE_FILES = json.loads((FIXTURES / "sonarr_episodefiles.json").read_text())
QUEUE = json.loads((FIXTURES / "sonarr_queue.json").read_text())


def make_http(handler):
    return CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_list_series_shapes_fixture_and_drops_empty_specials(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.url.path == "/api/v3/series"
        assert request.headers["X-Api-Key"] == "sk"
        return httpx.Response(200, json=SERIES)

    http = make_http(handler)
    series = await sonarr.list_series(http, s)
    assert len(captured) == 1
    assert len(series) == 2

    the_bear = series[0]
    assert the_bear["arr_id"] == 201
    assert the_bear["title"] == "The Bear"
    assert the_bear["year"] == 2022
    assert the_bear["tvdb_id"] == 371980
    assert the_bear["size_bytes"] == 10737418240
    assert the_bear["episode_count"] == 18
    assert the_bear["profile_id"] == 4
    assert the_bear["poster"] == "https://image.tmdb.org/t/p/original/poster-bear.jpg"
    assert the_bear["added"] == "2023-06-01T00:00:00Z"
    # Season 0 (specials) has episodeFileCount 0 -> dropped by the filter.
    assert [season["season_number"] for season in the_bear["seasons"]] == [1, 2]
    assert the_bear["seasons"][0] == {
        "season_number": 1,
        "size_bytes": 5368709120,
        "episode_file_count": 8,
        "monitored": True,
    }

    doctor_who = series[1]
    # Season 0 (specials) HAS files -> must be kept.
    assert [season["season_number"] for season in doctor_who["seasons"]] == [0, 1]
    assert doctor_who["seasons"][0] == {
        "season_number": 0,
        "size_bytes": 1073741824,
        "episode_file_count": 3,
        "monitored": True,
    }


async def test_episode_files_shapes_fixture(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.url.path == "/api/v3/episodefile"
        assert request.url.params["seriesId"] == "201"
        return httpx.Response(200, json=EPISODE_FILES)

    http = make_http(handler)
    files = await sonarr.episode_files(http, s, 201)
    assert len(captured) == 1
    assert files[0] == {
        "id": 3001,
        "season_number": 1,
        "size_bytes": 671088640,
        "quality": "WEBDL-1080p",
        "resolution": 1080,
    }
    assert [f["season_number"] for f in files] == [1, 1, 2]


async def test_get_queue_pct_math_including_zero_size(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.url.path == "/api/v3/queue"
        assert request.url.params["pageSize"] == "100"
        return httpx.Response(200, json=QUEUE)

    http = make_http(handler)
    queue = await sonarr.get_queue(http, s)
    assert len(captured) == 1
    assert queue[0] == {
        "tvdb_id": 371980,
        "title": "The.Bear.S03E01.1080p.WEB-DL",
        "size": 2147483648,
        "sizeleft": 536870912,
        "timeleft": "00:20:00",
        "status": "downloading",
        "pct": 75,
    }
    # size=0 must not raise ZeroDivisionError and must report pct=0.
    assert queue[1] == {
        "tvdb_id": 78804,
        "title": "Doctor.Who.S14E00.Special",
        "size": 0,
        "sizeleft": 0,
        "timeleft": "00:00:00",
        "status": "queued",
        "pct": 0,
    }


async def test_get_series_raw(tmp_path):
    s = make_settings(tmp_path)

    def handler(request):
        assert request.url.path == "/api/v3/series/201"
        assert request.method == "GET"
        return httpx.Response(200, json=SERIES[0])

    http = make_http(handler)
    series = await sonarr.get_series(http, s, 201)
    assert series == SERIES[0]


async def test_set_profile_get_then_put(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        if request.method == "GET":
            assert request.url.path == "/api/v3/series/201"
            return httpx.Response(200, json=SERIES[0])
        assert request.method == "PUT"
        assert request.url.path == "/api/v3/series/201"
        body = json.loads(request.content)
        assert body["qualityProfileId"] == 5
        assert body["title"] == "The Bear"
        assert body["id"] == 201
        return httpx.Response(200, json=body)

    http = make_http(handler)
    result = await sonarr.set_profile(http, s, 201, 5)
    assert result is None
    assert len(captured) == 2
    assert captured[0].method == "GET"
    assert captured[1].method == "PUT"


async def test_search_series_posts_command(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v3/command"
        body = json.loads(request.content)
        assert body == {"name": "SeriesSearch", "seriesId": 201}
        return httpx.Response(200, json={"id": 1})

    http = make_http(handler)
    result = await sonarr.search_series(http, s, 201)
    assert result is None
    assert len(captured) == 1


async def test_search_season_posts_command(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v3/command"
        body = json.loads(request.content)
        assert body == {"name": "SeasonSearch", "seriesId": 201, "seasonNumber": 1}
        return httpx.Response(200, json={"id": 2})

    http = make_http(handler)
    result = await sonarr.search_season(http, s, 201, 1)
    assert result is None
    assert len(captured) == 1


async def test_delete_series_sends_params(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.method == "DELETE"
        assert request.url.path == "/api/v3/series/201"
        assert request.url.params["deleteFiles"] == "true"
        return httpx.Response(200)

    http = make_http(handler)
    result = await sonarr.delete_series(http, s, 201)
    assert result is None
    assert len(captured) == 1


async def test_delete_season_deletes_exact_files_and_unmonitors_only_that_season(tmp_path):
    s = make_settings(tmp_path)
    captured = []
    deleted_ids = []

    def handler(request):
        captured.append(request)
        if request.url.path == "/api/v3/episodefile" and request.method == "GET":
            assert request.url.params["seriesId"] == "201"
            return httpx.Response(200, json=EPISODE_FILES)
        if request.url.path.startswith("/api/v3/episodefile/") and request.method == "DELETE":
            deleted_ids.append(int(request.url.path.rsplit("/", 1)[-1]))
            return httpx.Response(200)
        if request.url.path == "/api/v3/series/201" and request.method == "GET":
            return httpx.Response(200, json=SERIES[0])
        if request.url.path == "/api/v3/series/201" and request.method == "PUT":
            body = json.loads(request.content)
            seasons_by_number = {season["seasonNumber"]: season for season in body["seasons"]}
            # Season 1 (the one being deleted) must be unmonitored.
            assert seasons_by_number[1]["monitored"] is False
            # Other seasons must be untouched.
            assert seasons_by_number[0]["monitored"] is False  # unchanged from fixture
            assert seasons_by_number[2]["monitored"] is True  # unchanged from fixture
            return httpx.Response(200, json=body)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    http = make_http(handler)
    count = await sonarr.delete_season(http, s, 201, 1)

    # Exactly the season-1 episode file ids were deleted (3001, 3002), not 3003 (season 2).
    assert sorted(deleted_ids) == [3001, 3002]
    assert count == 2


def test_shape_series_survives_a_poster_image_without_remote_url(tmp_path):
    """One malformed image record must not 500 /library/series."""
    shaped = sonarr.shape_series(
        [
            {
                "id": 999, "title": "Broken Poster", "year": 2020, "tvdbId": 1,
                "qualityProfileId": 4,
                "statistics": {"sizeOnDisk": 10, "episodeFileCount": 1},
                "images": [{"coverType": "poster"}],
                "seasons": [
                    {"seasonNumber": 1, "monitored": True,
                     "statistics": {"sizeOnDisk": 10, "episodeFileCount": 1}}
                ],
            }
        ]
    )
    assert shaped[0]["poster"] is None
