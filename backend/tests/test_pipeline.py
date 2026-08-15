from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from pensieve.auth import SESSION_COOKIE, sign_session
from pensieve.clients.base import CachedHTTP
from pensieve.main import create_app
from pensieve.services import pipeline
from tests.conftest import make_settings, seed_user

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


def _request(**overrides):
    base = {
        "id": 1,
        "media_type": "movie",
        "tmdb_id": 100,
        "tvdb_id": None,
        "status": 3,
        "requested_by": "Sam",
        "created_at": "2026-08-10T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def _queue_row(**overrides):
    base = {
        "tmdb_id": None,
        "tvdb_id": None,
        "title": "Some.Release",
        "size": 1000,
        "sizeleft": 500,
        "timeleft": "01:00:00",
        "status": "downloading",
        "pct": 50,
    }
    base.update(overrides)
    return base


def test_movie_downloading_matches_on_tmdb_id_with_pct():
    req = _request(media_type="movie", tmdb_id=438631, status=3)
    radarr_q = [
        _queue_row(tmdb_id=438631, title="Dune.2021.2160p.WEB-DL", size=100, sizeleft=25, pct=75, timeleft="00:30:00", status="downloading")
    ]

    cards = pipeline.build([req], radarr_q, [], now=NOW)

    assert len(cards) == 1
    card = cards[0]
    assert card["status"] == "downloading"
    assert card["title"] == "Dune.2021.2160p.WEB-DL"
    assert card["pct"] == 75
    assert card["timeleft"] == "00:30:00"
    assert card["count"] == 1
    assert card["warning"] is None
    assert card["media_type"] == "movie"
    assert card["requested_by"] == "Sam"
    assert card["created_at"] == req["created_at"]


def test_tv_multi_episode_weighted_pct_and_count_and_max_timeleft():
    req = _request(media_type="tv", tmdb_id=None, tvdb_id=371980, status=3)
    sonarr_q = [
        _queue_row(tvdb_id=371980, title="The.Bear.S03E01", size=100, sizeleft=50, pct=50, timeleft="00:20:00", status="downloading"),
        _queue_row(tvdb_id=371980, title="The.Bear.S03E02", size=300, sizeleft=0, pct=90, timeleft="1.02:00:00", status="downloading"),
    ]

    cards = pipeline.build([req], [], sonarr_q, now=NOW)

    card = cards[0]
    # weighted pct = (50*100 + 90*300) / 400 = (5000 + 27000) / 400 = 80
    assert card["pct"] == 80
    assert card["count"] == 2
    # 1.02:00:00 (1 day 2h = 93600s) beats 00:20:00 (1200s)
    assert card["timeleft"] == "1.02:00:00"
    assert card["status"] == "downloading"
    assert card["title"] == "The.Bear.S03E01"


def test_stalled_queue_item_sets_warning_but_keeps_jellyseerr_stage():
    req = _request(media_type="movie", tmdb_id=872585, status=3)
    radarr_q = [_queue_row(tmdb_id=872585, title="Oppenheimer", status="stalled")]

    cards = pipeline.build([req], radarr_q, [], now=NOW)

    card = cards[0]
    assert card["status"] == "processing"  # STATUS_LABELS[3]
    assert card["warning"] == "stalled"
    assert card["title"] == "Oppenheimer"
    assert card["pct"] is None
    assert card["timeleft"] is None
    assert card["count"] is None


def test_available_request_recent_is_kept():
    req = _request(status=5, created_at=(NOW - timedelta(days=2)).isoformat())

    cards = pipeline.build([req], [], [], now=NOW)

    assert len(cards) == 1
    assert cards[0]["status"] == "available"


def test_available_request_older_than_14_days_is_dropped():
    req = _request(status=5, created_at=(NOW - timedelta(days=15)).isoformat())

    cards = pipeline.build([req], [], [], now=NOW)

    assert cards == []


def test_available_request_exactly_14_days_is_kept():
    req = _request(status=5, created_at=(NOW - timedelta(days=14)).isoformat())

    cards = pipeline.build([req], [], [], now=NOW)

    assert len(cards) == 1


def test_unmatched_processing_request_stays_processing():
    req = _request(media_type="movie", tmdb_id=999, status=3)

    cards = pipeline.build([req], [], [], now=NOW)

    card = cards[0]
    assert card["status"] == "processing"
    assert card["title"] is None
    assert card["pct"] is None
    assert card["timeleft"] is None
    assert card["warning"] is None
    assert card["count"] is None


def test_queue_row_with_none_id_never_matches():
    # A request with tmdb_id=None must never match, even against a queue row
    # that also has tmdb_id=None.
    req = _request(media_type="movie", tmdb_id=None, status=3)
    radarr_q = [_queue_row(tmdb_id=None, title="Untagged.Release", status="downloading")]

    cards = pipeline.build([req], radarr_q, [], now=NOW)

    card = cards[0]
    assert card["status"] == "processing"
    assert card["title"] is None
    assert card["pct"] is None


def test_tv_request_never_matches_radarr_queue():
    req = _request(media_type="tv", tmdb_id=None, tvdb_id=371980, status=3)
    # Same id present in radarr_q (wrong list) should not match.
    radarr_q = [_queue_row(tmdb_id=371980, title="Wrong.List", status="downloading")]

    cards = pipeline.build([req], radarr_q, [], now=NOW)

    card = cards[0]
    assert card["status"] == "processing"
    assert card["title"] is None


def test_downloading_row_with_none_size_does_not_crash():
    # A queue row can have "size"/"pct" present-but-None (not just absent).
    # `.get("size", 0)` alone doesn't guard that -- must be `or 0`.
    req = _request(media_type="movie", tmdb_id=438631, status=3)
    radarr_q = [
        _queue_row(tmdb_id=438631, title="Broken.Metadata", size=None, sizeleft=None, pct=None, status="downloading")
    ]

    cards = pipeline.build([req], radarr_q, [], now=NOW)

    card = cards[0]
    assert card["status"] == "downloading"
    assert card["pct"] == 0
    assert card["count"] == 1


def test_downloading_rows_with_none_and_zero_size_mixed_no_crash():
    req = _request(media_type="movie", tmdb_id=438631, status=3)
    radarr_q = [
        _queue_row(tmdb_id=438631, title="No.Size.Yet", size=None, pct=None, status="downloading"),
        _queue_row(tmdb_id=438631, title="Zero.Size", size=0, pct=0, status="downloading"),
        _queue_row(tmdb_id=438631, title="Real.Progress", size=100, sizeleft=25, pct=75, status="downloading"),
    ]

    cards = pipeline.build([req], radarr_q, [], now=NOW)

    card = cards[0]
    assert card["status"] == "downloading"
    # Only the real row contributes size/weight: (75*100) / 100 = 75.
    assert card["pct"] == 75
    assert card["count"] == 3


def test_available_request_z_suffix_created_at_older_than_14_days_is_dropped():
    # Jellyseerr's raw createdAt is like "2024-08-01T10:30:00Z"; shape_requests
    # passes that literal string through, so build() must parse "Z" directly.
    req = _request(status=5, created_at="2026-07-01T10:00:00.000Z")

    cards = pipeline.build([req], [], [], now=NOW)

    assert cards == []


def test_unknown_status_code_falls_back_to_unknown():
    req = _request(media_type="movie", tmdb_id=999, status=6)

    cards = pipeline.build([req], [], [], now=NOW)

    assert cards[0]["status"] == "unknown"


# --- Route tests: GET /api/pipeline -----------------------------------------

_RECENT = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

RAW_JELLYSEERR = {
    "results": [
        {
            "id": 1,
            "createdAt": _RECENT,
            "media": {"mediaType": "movie", "tmdbId": 438631, "tvdbId": None, "status": 3},
            "requestedBy": {"displayName": "Sam", "plexUsername": None},
        },
        {
            "id": 2,
            "createdAt": _RECENT,
            "media": {"mediaType": "tv", "tmdbId": None, "tvdbId": 371980, "status": 3},
            "requestedBy": {"displayName": None, "plexUsername": "alexplex"},
        },
    ]
}

RAW_RADARR_QUEUE = {
    "records": [
        {
            "id": 5001,
            "title": "Dune.2021.2160p.WEB-DL",
            "size": 100,
            "sizeleft": 25,
            "timeleft": "00:30:00",
            "status": "downloading",
            "movie": {"tmdbId": 438631},
        }
    ]
}

RAW_SONARR_QUEUE = {
    "records": [
        {
            "id": 6001,
            "title": "The.Bear.S03E01",
            "size": 200,
            "sizeleft": 50,
            "timeleft": "00:20:00",
            "status": "downloading",
            "series": {"tvdbId": 371980},
        }
    ]
}


def _healthy_route(request: httpx.Request) -> httpx.Response:
    if request.url.host == "jellyseerr":
        return httpx.Response(200, json=RAW_JELLYSEERR)
    if request.url.host == "radarr":
        return httpx.Response(200, json=RAW_RADARR_QUEUE)
    if request.url.host == "sonarr":
        return httpx.Response(200, json=RAW_SONARR_QUEUE)
    return httpx.Response(404)


def _radarr_500_route(request: httpx.Request) -> httpx.Response:
    if request.url.host == "jellyseerr":
        return httpx.Response(200, json=RAW_JELLYSEERR)
    if request.url.host == "radarr":
        return httpx.Response(500, text="boom")
    if request.url.host == "sonarr":
        return httpx.Response(200, json=RAW_SONARR_QUEUE)
    return httpx.Response(404)


def _all_down_route(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="boom")


class _ToggleTransport(httpx.AsyncBaseTransport):
    """A transport whose routing function can be swapped mid-test.

    Lets a test prime a warm ``CachedHTTP`` cache against a healthy backend,
    then flip to a failing backend on the *same* ``CachedHTTP`` instance so
    the previously-cached entries are still there for ``stale()`` to find.
    """

    def __init__(self, route):
        self.route = route

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self.route(request)


def _make_client(tmp_path, route, **overrides) -> tuple[TestClient, object, _ToggleTransport]:
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    transport = _ToggleTransport(route)
    app.state.http = CachedHTTP(httpx.AsyncClient(transport=transport))
    return client, settings, transport


def _login(client: TestClient, settings) -> None:
    # current_user is DB-authoritative, so the users row has to exist too.
    seed_user(settings, user_id=1, name="Sam", role="member")
    client.cookies.set(
        SESSION_COOKIE, sign_session(settings, {"id": 1, "name": "Sam", "role": "member"})
    )


def test_pipeline_requires_auth(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _healthy_route)
    try:
        resp = client.get("/api/pipeline")
        assert resp.status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_pipeline_happy_path_joins_requests_with_queues(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _healthy_route)
    try:
        _login(client, settings)
        resp = client.get("/api/pipeline")
        assert resp.status_code == 200
        body = resp.json()
        assert "stale_seconds" not in body
        cards = body["cards"]
        assert len(cards) == 2

        movie_card = next(c for c in cards if c["media_type"] == "movie")
        assert movie_card["status"] == "downloading"
        assert movie_card["title"] == "Dune.2021.2160p.WEB-DL"
        assert movie_card["pct"] == 75
        assert movie_card["requested_by"] == "Sam"

        tv_card = next(c for c in cards if c["media_type"] == "tv")
        assert tv_card["status"] == "downloading"
        assert tv_card["title"] == "The.Bear.S03E01"
        assert tv_card["pct"] == 75
        assert tv_card["requested_by"] == "alexplex"
    finally:
        client.__exit__(None, None, None)


def test_pipeline_falls_back_to_stale_when_radarr_down_but_cache_warm(tmp_path, monkeypatch):
    # get_queue/list_requests all use a 30s TTL, so a same-tick second call
    # would be served straight from the fresh cache without ever exercising
    # the fallback path. Fake the clock forward past the TTL so the second
    # call actually attempts (and fails) a live radarr request, forcing the
    # stale() fallback.
    clock = {"t": 1_000.0}
    monkeypatch.setattr("pensieve.clients.base.time.monotonic", lambda: clock["t"])

    client, settings, transport = _make_client(tmp_path, _healthy_route)
    try:
        _login(client, settings)
        warm = client.get("/api/pipeline")
        assert warm.status_code == 200

        transport.route = _radarr_500_route
        clock["t"] += 40

        resp = client.get("/api/pipeline")
        assert resp.status_code == 200
        body = resp.json()
        assert "stale_seconds" in body
        assert body["stale_seconds"] == 40

        cards = body["cards"]
        movie_card = next(c for c in cards if c["media_type"] == "movie")
        assert movie_card["status"] == "downloading"
        assert movie_card["pct"] == 75
    finally:
        client.__exit__(None, None, None)


def test_pipeline_502_when_all_upstreams_down_and_cache_cold(tmp_path):
    client, settings, _transport = _make_client(tmp_path, _all_down_route)
    try:
        _login(client, settings)
        resp = client.get("/api/pipeline")
        assert resp.status_code == 502
        assert "jellyseerr" in resp.json()["error"]
    finally:
        client.__exit__(None, None, None)


def test_enrich_media_fills_missing_titles_from_library_maps():
    requests = [
        {"media_type": "movie", "tmdb_id": 603, "tvdb_id": None},
        {"media_type": "tv", "tmdb_id": None, "tvdb_id": 81189},
        {"media_type": "tv", "tmdb_id": None, "tvdb_id": 99999},  # not in library
    ]
    enriched = pipeline.enrich_media(
        requests,
        movie_titles={603: "The Matrix"},
        series_titles={81189: "Breaking Bad"},
    )
    assert enriched[0]["title"] == "The Matrix"
    assert enriched[1]["title"] == "Breaking Bad"
    assert enriched[2].get("title") is None
    # pure: input untouched
    assert "title" not in requests[0]


def test_build_uses_enriched_request_title_when_no_queue_match():
    requests = pipeline.enrich_media(
        [{"media_type": "tv", "tvdb_id": 81189, "tmdb_id": None, "status": 4,
          "requested_by": "Sam", "created_at": NOW.isoformat()}],
        movie_titles={},
        series_titles={81189: "Breaking Bad"},
    )
    cards = pipeline.build(requests, [], [], now=NOW)
    assert cards[0]["title"] == "Breaking Bad"
    assert cards[0]["status"] == "partially_available"


def test_build_prefers_queue_title_over_enriched_title():
    requests = pipeline.enrich_media(
        [{"media_type": "tv", "tvdb_id": 81189, "tmdb_id": None, "status": 3,
          "requested_by": "Sam", "created_at": NOW.isoformat()}],
        movie_titles={},
        series_titles={81189: "Breaking Bad"},
    )
    queue = [{"tvdb_id": 81189, "title": "Breaking.Bad.S05E14.1080p", "status": "downloading",
              "size": 100, "sizeleft": 50, "pct": 50, "timeleft": "00:10:00"}]
    cards = pipeline.build(requests, [], queue, now=NOW)
    assert cards[0]["title"] == "Breaking.Bad.S05E14.1080p"


def test_enrich_media_falls_back_to_hint_titles_for_fresh_requests():
    requests = pipeline.enrich_media(
        [{"media_type": "movie", "tmdb_id": 999, "tvdb_id": None}],
        movie_titles={},
        series_titles={},
        hints={("movie", 999): {"title": "Brand New Movie"}},
    )
    assert requests[0]["title"] == "Brand New Movie"


# ------------------------------------------------------------------ posters


def test_enrich_media_fills_posters_from_library_maps():
    requests = [
        {"media_type": "movie", "tmdb_id": 603, "tvdb_id": None},
        {"media_type": "tv", "tmdb_id": None, "tvdb_id": 81189},
        {"media_type": "tv", "tmdb_id": None, "tvdb_id": 99999},  # not in library
    ]

    enriched = pipeline.enrich_media(
        requests,
        movie_titles={603: "The Matrix"},
        series_titles={81189: "Breaking Bad"},
        movie_posters={603: "https://image.tmdb.org/t/p/original/matrix.jpg"},
        series_posters={81189: "https://artworks.thetvdb.com/bb.jpg"},
    )

    assert enriched[0]["poster"] == "https://image.tmdb.org/t/p/original/matrix.jpg"
    assert enriched[1]["poster"] == "https://artworks.thetvdb.com/bb.jpg"
    assert enriched[2].get("poster") is None
    # pure: input untouched
    assert "poster" not in requests[0]


def test_enrich_media_falls_back_to_hint_poster_for_a_fresh_request():
    """A request made minutes ago is in no arr library yet, but Discover saw it."""
    enriched = pipeline.enrich_media(
        [{"media_type": "movie", "tmdb_id": 999, "tvdb_id": None}],
        movie_titles={},
        series_titles={},
        hints={("movie", 999): {"title": "Brand New Movie", "poster": "/fresh.jpg"}},
    )

    assert enriched[0]["title"] == "Brand New Movie"
    assert enriched[0]["poster"] == "/fresh.jpg"


def test_build_carries_poster_and_tmdb_id_onto_the_card():
    """The board is poster-led, and a tile has to be able to open its detail sheet."""
    req = _request(media_type="movie", tmdb_id=603, status=3)
    req["poster"] = "https://image.tmdb.org/t/p/original/matrix.jpg"

    cards = pipeline.build([req], [], [], now=NOW)

    assert cards[0]["poster"] == "https://image.tmdb.org/t/p/original/matrix.jpg"
    assert cards[0]["tmdb_id"] == 603


def test_build_emits_null_poster_when_nothing_supplied_one():
    """No artwork anywhere is a stone placeholder, not a missing key."""
    cards = pipeline.build([_request()], [], [], now=NOW)

    assert cards[0]["poster"] is None


RAW_RADARR_LIBRARY = [
    {
        "id": 1,
        "title": "Dune",
        "year": 2021,
        "tmdbId": 438631,
        "sizeOnDisk": 0,
        "qualityProfileId": 4,
        "hasFile": False,
        "images": [{"coverType": "poster", "remoteUrl": "https://img.example/dune.jpg"}],
    }
]

RAW_SONARR_LIBRARY = [
    {
        "id": 2,
        "title": "The Bear",
        "year": 2022,
        "tvdbId": 371980,
        "statistics": {"sizeOnDisk": 0, "episodeFileCount": 0},
        "qualityProfileId": 4,
        "seasons": [],
        "images": [{"coverType": "poster", "remoteUrl": "https://img.example/bear.jpg"}],
    }
]


def _library_route(request: httpx.Request) -> httpx.Response:
    """Healthy upstreams that also answer the arr *library* endpoints.

    ``_healthy_route`` answers every radarr/sonarr path with a queue body, so
    title/poster enrichment silently fails there (by design -- it is wrapped
    in a best-effort except). This route distinguishes the paths, which is
    what makes enrichment observable over HTTP.
    """
    if request.url.host == "jellyseerr":
        return httpx.Response(200, json=RAW_JELLYSEERR)
    if request.url.host == "radarr":
        if request.url.path.endswith("/queue"):
            return httpx.Response(200, json=RAW_RADARR_QUEUE)
        return httpx.Response(200, json=RAW_RADARR_LIBRARY)
    if request.url.host == "sonarr":
        if request.url.path.endswith("/queue"):
            return httpx.Response(200, json=RAW_SONARR_QUEUE)
        return httpx.Response(200, json=RAW_SONARR_LIBRARY)
    return httpx.Response(404)


def test_pipeline_route_serves_posters_and_tmdb_ids(tmp_path):
    """Over HTTP, not just through the mapper -- the layer a 422 hides in."""
    client, settings, _transport = _make_client(tmp_path, _library_route)
    try:
        _login(client, settings)
        resp = client.get("/api/pipeline")
        assert resp.status_code == 200
        cards = resp.json()["cards"]

        movie_card = next(c for c in cards if c["media_type"] == "movie")
        assert movie_card["poster"] == "https://img.example/dune.jpg"
        assert movie_card["tmdb_id"] == 438631

        tv_card = next(c for c in cards if c["media_type"] == "tv")
        assert tv_card["poster"] == "https://img.example/bear.jpg"
    finally:
        client.__exit__(None, None, None)
