"""Tests for the actor surface: person shaping, suggestions, and filmography.

Fixtures are trimmed captures of a live Jellyseerr 2.x (2026-08-18) for TMDB
person 31, so the field names here are the real ones -- ``profilePath``,
``cast``/``crew``, ``creditId`` -- rather than a guess at the schema. The cast
slice is deliberately *not* in popularity order upstream, and deliberately
carries the same title twice under two credit ids, because those are the two
things the shaping has to fix.
"""
import json
from pathlib import Path

import httpx
import pytest

from pensieve.clients import jellyseerr
from pensieve.clients.base import CachedHTTP, UpstreamError
from pensieve.services import discover
from tests.conftest import make_settings

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "jellyseerr_search.json").read_text())
PERSON = json.loads((FIXTURES / "jellyseerr_person.json").read_text())
CREDITS = json.loads((FIXTURES / "jellyseerr_person_credits.json").read_text())


def make_http(handler):
    return CachedHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# --- person shaping ----------------------------------------------------------


def test_shape_person():
    """The person row the old shaping dropped on the floor."""
    assert jellyseerr.shape_person(SEARCH["results"][2]) == {
        "person_id": 4381244,
        "name": "Aurora Matrix",
        "profile_path": "/l5ho43eQhxEEz7MEavjsieA7S1W.jpg",
        "media_type": "person",
    }


def test_shape_person_keeps_a_missing_headshot_as_null():
    shaped = jellyseerr.shape_person({"id": 7, "mediaType": "person", "name": "Nobody"})
    assert shaped == {
        "person_id": 7, "name": "Nobody", "profile_path": None, "media_type": "person",
    }


@pytest.mark.parametrize(
    "row",
    [
        SEARCH["results"][0],                                  # a film
        {"id": 7, "mediaType": "person"},                      # nameless
        {"id": 7, "mediaType": "person", "name": ""},          # blank name
        {"mediaType": "person", "name": "No Id"},              # unidentifiable
        {"id": "7", "mediaType": "person", "name": "Str Id"},  # id is not an int
    ],
    ids=["a-film", "no-name", "blank-name", "no-id", "string-id"],
)
def test_shape_person_refuses_anything_that_is_not_a_person(row):
    assert jellyseerr.shape_person(row) is None


# --- suggestions -------------------------------------------------------------


def test_shape_suggestions_mixes_people_in_at_their_upstream_rank():
    """Relevance order is upstream\'s to decide, and it gets it right.

    Pinning people to the top would put "Aurora Matrix" above "The Matrix" for
    the query *matrix*, which is exactly backwards. Order is preserved.
    """
    shaped = jellyseerr.shape_suggestions(SEARCH)
    assert [row["media_type"] for row in shaped] == ["movie", "tv", "person"]
    assert shaped[2]["name"] == "Aurora Matrix"
    # Title rows are the same cards the grid renders, not a second shape.
    assert shaped[0] == jellyseerr.shape_media(SEARCH["results"][0])


def test_shape_suggestions_caps_at_the_limit():
    raw = {"results": [dict(SEARCH["results"][0], id=i) for i in range(50)]}
    assert len(jellyseerr.shape_suggestions(raw, limit=8)) == 8


def test_shape_suggestions_counts_only_what_it_keeps():
    """An unusable row must not eat one of the eight slots."""
    junk = {"id": None, "mediaType": "movie"}
    raw = {"results": [junk, junk, *SEARCH["results"]]}
    assert len(jellyseerr.shape_suggestions(raw, limit=3)) == 3


def test_shape_suggestions_tolerates_a_missing_results_key():
    assert jellyseerr.shape_suggestions({}) == []


async def test_suggest_reuses_the_search_cache_entry(tmp_path):
    """Typing costs one upstream call, not two.

    The dropdown and the results grid ask two different app routes for the
    same query, and both land on Jellyseerr\'s ``/search``. They share a cache
    key only if they send byte-identical params -- which is why ``suggest``
    goes through the same helper as ``search`` rather than building its own
    request.
    """
    calls: list[httpx.URL] = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=SEARCH)

    http, settings = make_http(handler), make_settings(tmp_path)
    await jellyseerr.search(http, settings, "matrix", ttl=60)
    await jellyseerr.suggest(http, settings, "matrix", ttl=60)

    assert len(calls) == 1


async def test_suggest_shapes_and_caps(tmp_path):
    http = make_http(lambda request: httpx.Response(200, json=SEARCH))
    rows = await jellyseerr.suggest(http, make_settings(tmp_path), "matrix", limit=2)
    assert [row["media_type"] for row in rows] == ["movie", "tv"]


async def test_suggest_percent_encodes_a_multi_word_query(tmp_path):
    """The dropdown\'s whole reason for existing is queries like *tom hanks*."""
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=SEARCH)

    await jellyseerr.suggest(make_http(handler), make_settings(tmp_path), "tom hanks")

    assert "query=tom%20hanks" in seen[0]
    assert "+" not in seen[0]


async def test_suggest_propagates_upstream_failure(tmp_path):
    http = make_http(lambda request: httpx.Response(500))
    with pytest.raises(UpstreamError):
        await jellyseerr.suggest(http, make_settings(tmp_path), "matrix")


# --- person detail -----------------------------------------------------------


async def test_person_returns_the_name_and_headshot(tmp_path):
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=PERSON)

    http = make_http(handler)
    assert await jellyseerr.person(http, make_settings(tmp_path), 31) == {
        "person_id": 31,
        "name": "Tom Hanks",
        "profile_path": "/oFvZoKI6lvU03n4YoNGAll9rkas.jpg",
        "media_type": "person",
    }
    assert seen == ["/api/v1/person/31"]


# --- filmography -------------------------------------------------------------


async def test_person_credits_are_acting_only(tmp_path):
    """``crew`` is a different question. The fixture\'s only crew row is a
    directing credit for a film absent from ``cast``; if it appears, the
    implementation read the wrong array."""
    http = make_http(lambda request: httpx.Response(200, json=CREDITS))
    items = await jellyseerr.person_credits(http, make_settings(tmp_path), 31)

    assert CREDITS["crew"][0]["id"] not in {item["tmdb_id"] for item in items}
    assert CREDITS["crew"][0]["title"] not in {item["title"] for item in items}


async def test_person_credits_sorted_by_vote_count_not_upstream_order(tmp_path):
    http = make_http(lambda request: httpx.Response(200, json=CREDITS))
    items = await jellyseerr.person_credits(http, make_settings(tmp_path), 31)

    assert [item["title"] for item in items] == [
        "Forrest Gump",         # 30,232 votes
        "Toy Story",            # 20,344
        "Saving Private Ryan",  # 17,614
        "The Daily Show",       #    651
        "Family Ties",          #    275
    ]


async def test_person_credits_rank_films_above_trending_talk_shows(tmp_path):
    """The 1.2.0 bug this fixture exists to pin.

    TMDB\'s ``popularity`` is a rolling *trending* score, not a measure of how
    significant a credit is, so a daily talk show someone guested on once
    outscores everything they are actually known for -- by an order of
    magnitude here (The Daily Show 179.8 against Forrest Gump\'s 29.4).
    Sorting by it filled the filmography with chat-show appearances: only ten
    of Tom Hanks\'s fifty rows were films. ``voteCount`` is how many people
    cared enough to rate the thing, which does not decay, and it puts the
    films back on top.
    """
    http = make_http(lambda request: httpx.Response(200, json=CREDITS))
    titles = [item["title"] for item in await jellyseerr.person_credits(
        http, make_settings(tmp_path), 31)]

    talk_show = titles.index("The Daily Show")
    for film in ("Forrest Gump", "Toy Story", "Saving Private Ryan"):
        assert titles.index(film) < talk_show, f"{film} must outrank the talk show"


async def test_person_credits_dedupes_a_title_credited_twice(tmp_path):
    """TMDB lists one title once per credit id, so a second role is a second row."""
    http = make_http(lambda request: httpx.Response(200, json=CREDITS))
    items = await jellyseerr.person_credits(http, make_settings(tmp_path), 31)

    keys = [(item["media_type"], item["tmdb_id"]) for item in items]
    assert len(keys) == len(set(keys))


async def test_person_credits_keeps_a_movie_and_a_show_that_share_a_tmdb_id(tmp_path):
    """Movie 13 and show 13 are two different titles, not a duplicate.

    TMDB\'s id space is per media type, so de-duplicating on the bare id would
    silently drop one of them.
    """
    body = {
        "cast": [
            {"id": 13, "mediaType": "movie", "title": "Film Thirteen", "voteCount": 9},
            {"id": 13, "mediaType": "tv", "name": "Show Thirteen", "voteCount": 8},
        ],
        "crew": [],
        "id": 31,
    }
    http = make_http(lambda request: httpx.Response(200, json=body))
    items = await jellyseerr.person_credits(http, make_settings(tmp_path), 31)

    assert [item["media_type"] for item in items] == ["movie", "tv"]


async def test_person_credits_carries_availability_from_media_info(tmp_path):
    """A filmography tile has to be able to say "already here" like any other."""
    http = make_http(lambda request: httpx.Response(200, json=CREDITS))
    items = await jellyseerr.person_credits(http, make_settings(tmp_path), 31)

    by_title = {item["title"]: item for item in items}
    assert by_title["Saving Private Ryan"]["availability"] == "available"
    assert by_title["Forrest Gump"]["availability"] == "requestable"


async def test_person_credits_drops_rows_that_are_not_requestable_titles(tmp_path):
    """The fixture\'s planted person row tops *both* orderings -- highest
    popularity and highest vote count -- so a missing filter puts it first
    rather than nowhere, whichever key the sort is using."""
    http = make_http(lambda request: httpx.Response(200, json=CREDITS))
    items = await jellyseerr.person_credits(http, make_settings(tmp_path), 31)

    assert all(item["media_type"] in ("movie", "tv") for item in items)
    assert 999999 not in {item["tmdb_id"] for item in items}


async def test_person_credits_caps_at_fifty(tmp_path):
    body = {
        "cast": [
            {"id": i, "mediaType": "movie", "title": f"Film {i}", "voteCount": i}
            for i in range(200)
        ],
        "crew": [],
        "id": 31,
    }
    http = make_http(lambda request: httpx.Response(200, json=body))
    items = await jellyseerr.person_credits(http, make_settings(tmp_path), 31)

    assert len(items) == 50
    # The cap keeps the top of the list, not an arbitrary slice of it.
    assert items[0]["title"] == "Film 199"


async def test_person_credits_tolerates_a_missing_vote_count(tmp_path):
    body = {
        "cast": [
            {"id": 1, "mediaType": "movie", "title": "Unranked"},
            {"id": 2, "mediaType": "movie", "title": "Ranked", "voteCount": 5},
        ],
        "crew": [],
        "id": 31,
    }
    http = make_http(lambda request: httpx.Response(200, json=body))
    items = await jellyseerr.person_credits(http, make_settings(tmp_path), 31)

    assert [item["title"] for item in items] == ["Ranked", "Unranked"]


# --- the service that joins the two calls ------------------------------------


async def test_filmography_joins_the_name_to_the_credits(tmp_path):
    def handler(request):
        if request.url.path.endswith("/combined_credits"):
            return httpx.Response(200, json=CREDITS)
        return httpx.Response(200, json=PERSON)

    http = make_http(handler)
    result = await discover.filmography(http, make_settings(tmp_path), 31)

    assert result["name"] == "Tom Hanks"
    assert result["profile_path"] == "/oFvZoKI6lvU03n4YoNGAll9rkas.jpg"
    assert [item["title"] for item in result["items"]][0] == "Forrest Gump"


async def test_filmography_raises_when_the_person_is_unknown(tmp_path):
    """A 404 has to survive the join, or an unknown id reads as "no films"."""
    def handler(request):
        if request.url.path.endswith("/combined_credits"):
            return httpx.Response(200, json={"cast": [], "crew": [], "id": 1})
        return httpx.Response(404, json={"message": "Person not found"})

    http = make_http(handler)
    with pytest.raises(UpstreamError) as caught:
        await discover.filmography(http, make_settings(tmp_path), 1)
    assert caught.value.status == 404


async def test_filmography_raises_when_the_credits_call_fails(tmp_path):
    def handler(request):
        if request.url.path.endswith("/combined_credits"):
            return httpx.Response(503)
        return httpx.Response(200, json=PERSON)

    http = make_http(handler)
    with pytest.raises(UpstreamError):
        await discover.filmography(http, make_settings(tmp_path), 31)


async def test_filmography_answers_an_actor_with_no_acting_credits(tmp_path):
    """A director with no cast row is an empty filmography, not a failure."""
    def handler(request):
        if request.url.path.endswith("/combined_credits"):
            return httpx.Response(200, json={"cast": [], "crew": CREDITS["crew"], "id": 31})
        return httpx.Response(200, json=PERSON)

    http = make_http(handler)
    result = await discover.filmography(http, make_settings(tmp_path), 31)
    assert result == {
        "person_id": 31,
        "name": "Tom Hanks",
        "profile_path": "/oFvZoKI6lvU03n4YoNGAll9rkas.jpg",
        "items": [],
    }
