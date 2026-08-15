import httpx
import pytest

from pensieve.clients.base import CachedHTTP, UpstreamError


def make_http(handler) -> CachedHTTP:
    return CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_ttl_cache_hits_upstream_once():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"v": calls["n"]})

    http = make_http(handler)
    a = await http.get_json("http://x/api", service="radarr", ttl=60)
    b = await http.get_json("http://x/api", service="radarr", ttl=60)
    assert a == b == {"v": 1} and calls["n"] == 1


async def test_non_2xx_raises_upstream_error_with_service():
    http = make_http(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamError) as ei:
        await http.get_json("http://x/api", service="sonarr")
    assert ei.value.service == "sonarr"


async def test_stale_survives_upstream_failure():
    state = {"ok": True}

    def handler(request):
        return httpx.Response(200, json={"v": 1}) if state["ok"] \
            else httpx.Response(503)

    http = make_http(handler)
    await http.get_json("http://x/api", service="radarr", ttl=0.001)
    state["ok"] = False
    import asyncio; await asyncio.sleep(0.01)
    with pytest.raises(UpstreamError):
        await http.get_json("http://x/api", service="radarr", ttl=0.001)
    stale = http.stale("http://x/api")
    assert stale is not None and stale[0] == {"v": 1}


async def test_send_json_never_cached():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={})

    http = make_http(handler)
    await http.send_json("POST", "http://x/api", service="radarr", json={"a": 1})
    await http.send_json("POST", "http://x/api", service="radarr", json={"a": 1})
    assert calls["n"] == 2


async def test_invalidate_drops_matching_prefix():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"v": calls["n"]})

    http = make_http(handler)
    a = await http.get_json("http://x/api/movies", service="radarr", ttl=60)
    assert a == {"v": 1} and calls["n"] == 1

    http.invalidate("http://x/api")

    b = await http.get_json("http://x/api/movies", service="radarr", ttl=60)
    assert b == {"v": 2} and calls["n"] == 2


async def test_returned_values_are_isolated_from_cache():
    def handler(request):
        return httpx.Response(200, json={"v": 1, "nested": [1, 2]})

    http = make_http(handler)

    first = await http.get_json("http://x/api", service="radarr", ttl=60)
    first["v"] = "mutated"
    first["nested"].append(3)

    second = await http.get_json("http://x/api", service="radarr", ttl=60)
    assert second == {"v": 1, "nested": [1, 2]}

    stale_a = http.stale("http://x/api")
    assert stale_a is not None
    stale_a[0]["v"] = "also mutated"

    stale_b = http.stale("http://x/api")
    assert stale_b is not None and stale_b[0] == {"v": 1, "nested": [1, 2]}


async def test_get_text_returns_the_raw_body():
    http = make_http(lambda r: httpx.Response(200, text="<MediaContainer/>"))
    assert await http.get_text("http://x/api", service="plex.tv") == "<MediaContainer/>"


async def test_get_text_raises_upstream_error_with_service():
    http = make_http(lambda r: httpx.Response(503))
    with pytest.raises(UpstreamError) as ei:
        await http.get_text("http://x/api", service="plex.tv")
    assert ei.value.service == "plex.tv"
