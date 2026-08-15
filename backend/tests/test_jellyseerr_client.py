import json
from pathlib import Path

import httpx

from pensieve.clients.base import CachedHTTP
from pensieve.clients import jellyseerr
from tests.conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures"
REQUESTS = json.loads((FIXTURES / "jellyseerr_requests.json").read_text())


def make_http(handler):
    return CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_list_requests_shapes_fixture(tmp_path):
    s = make_settings(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        assert request.url.path == "/api/v1/request"
        assert request.url.params["take"] == "50"
        assert request.url.params["sort"] == "added"
        assert request.url.params["filter"] == "all"
        assert request.headers["X-Api-Key"] == "jk"
        return httpx.Response(200, json=REQUESTS)

    http = make_http(handler)
    requests = await jellyseerr.list_requests(http, s)
    assert len(captured) == 1
    assert len(requests) == 3

    # First request: movie with displayName
    movie1 = requests[0]
    assert movie1 == {
        "id": 1,
        "media_type": "movie",
        "tmdb_id": 550,
        "tvdb_id": None,
        "status": 5,
        "requested_by": "Sam",
        "created_at": "2024-08-01T10:30:00Z",
    }

    # Second request: TV show with tvdbId and plexUsername fallback
    tv_show = requests[1]
    assert tv_show == {
        "id": 2,
        "media_type": "tv",
        "tmdb_id": 1399,
        "tvdb_id": 121361,
        "status": 3,
        "requested_by": "alexplex",
        "created_at": "2024-08-02T14:15:00Z",
    }

    # Third request: movie with status 2
    movie2 = requests[2]
    assert movie2 == {
        "id": 3,
        "media_type": "movie",
        "tmdb_id": 278,
        "tvdb_id": None,
        "status": 2,
        "requested_by": "Ada",
        "created_at": "2024-08-03T08:45:00Z",
    }


async def test_status_labels_mapping(tmp_path):
    """Verify STATUS_LABELS constant has the correct status code mappings."""
    assert jellyseerr.STATUS_LABELS == {
        1: "requested",
        2: "requested",
        3: "processing",
        4: "partially_available",
        5: "available",
    }


async def test_list_requests_handles_missing_fields(tmp_path):
    """Verify list_requests gracefully handles records missing media or requestedBy."""
    s = make_settings(tmp_path)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    # Normal record
                    {
                        "id": 1,
                        "createdAt": "2024-08-01T10:30:00Z",
                        "media": {
                            "mediaType": "movie",
                            "tmdbId": 550,
                            "tvdbId": None,
                            "status": 5,
                        },
                        "requestedBy": {
                            "id": 1,
                            "displayName": "Sam",
                            "plexUsername": None,
                        },
                    },
                    # Missing requestedBy entirely
                    {
                        "id": 2,
                        "createdAt": "2024-08-02T14:15:00Z",
                        "media": {
                            "mediaType": "tv",
                            "tmdbId": 1399,
                            "tvdbId": 121361,
                            "status": 3,
                        },
                    },
                    # Missing media entirely
                    {
                        "id": 3,
                        "createdAt": "2024-08-03T08:45:00Z",
                        "requestedBy": {
                            "id": 3,
                            "displayName": "Ada",
                            "plexUsername": "adaplex",
                        },
                    },
                ],
                "pageInfo": {"pages": 1, "pageSize": 50, "results": 3, "totalResults": 3},
            },
        )

    http = make_http(handler)
    requests = await jellyseerr.list_requests(http, s)
    assert len(requests) == 3

    # Normal record unchanged
    assert requests[0] == {
        "id": 1,
        "media_type": "movie",
        "tmdb_id": 550,
        "tvdb_id": None,
        "status": 5,
        "requested_by": "Sam",
        "created_at": "2024-08-01T10:30:00Z",
    }

    # Missing requestedBy -> all user fields are None
    assert requests[1] == {
        "id": 2,
        "media_type": "tv",
        "tmdb_id": 1399,
        "tvdb_id": 121361,
        "status": 3,
        "requested_by": None,
        "created_at": "2024-08-02T14:15:00Z",
    }

    # Missing media -> all media fields are None
    assert requests[2] == {
        "id": 3,
        "media_type": None,
        "tmdb_id": None,
        "tvdb_id": None,
        "status": None,
        "requested_by": "Ada",
        "created_at": "2024-08-03T08:45:00Z",
    }
