"""The pure filter -> Jellyseerr parameter mapper.

Every rule that decides what the app asks upstream lives in one function
with no I/O, so all of it is pinned here without a network or a clock.
"""

from datetime import date

import pytest

from pensieve.services import browse

TODAY = date(2026, 8, 14)


def call(**overrides):
    """to_upstream with sensible defaults, overridden per test."""
    kwargs = dict(
        sort="popular", media="movie", genre=None,
        decade=None, min_rating=None, page=1, today=TODAY,
    )
    kwargs.update(overrides)
    return browse.to_upstream(**kwargs)


def test_popular_movies_is_popularity_desc():
    path, params = call()
    assert path == "/discover/movies"
    assert params == {"page": 1, "sortBy": "popularity.desc"}


def test_tv_uses_the_tv_path():
    path, _ = call(media="tv")
    assert path == "/discover/tv"


def test_media_defaults_to_movie_when_absent():
    """The route passes None for 'not supplied'; movie is the house default."""
    path, _ = call(media=None)
    assert path == "/discover/movies"


def test_newest_caps_at_today_so_unreleased_rows_stay_out():
    _, params = call(sort="newest")
    assert params["sortBy"] == "release_date.desc"
    assert params["primaryReleaseDateLte"] == "2026-08-14"


def test_newest_tv_uses_first_air_date_names():
    _, params = call(sort="newest", media="tv")
    assert params["sortBy"] == "first_air_date.desc"
    assert params["firstAirDateLte"] == "2026-08-14"
    assert "primaryReleaseDateLte" not in params


def test_upcoming_starts_at_today_and_sorts_ascending():
    _, params = call(sort="upcoming")
    assert params["sortBy"] == "release_date.asc"
    assert params["primaryReleaseDateGte"] == "2026-08-14"


def test_top_rated_carries_the_movie_vote_floor():
    _, params = call(sort="top_rated")
    assert params["sortBy"] == "vote_average.desc"
    assert params["voteCountGte"] == 300


def test_top_rated_tv_floor_is_lower():
    _, params = call(sort="top_rated", media="tv")
    assert params["voteCountGte"] == 100


def test_min_rating_always_brings_a_vote_floor_with_it():
    """voteAverageGte alone returns one-vote titles rated 10/10."""
    _, params = call(min_rating=8)
    assert params["voteAverageGte"] == 8
    assert params["voteCountGte"] == 300


def test_genre_passes_through():
    _, params = call(genre=27)
    assert params["genre"] == 27


@pytest.mark.parametrize(
    "decade,gte,lte",
    [
        ("2020s", "2020-01-01", "2029-12-31"),
        ("2010s", "2010-01-01", "2019-12-31"),
        ("2000s", "2000-01-01", "2009-12-31"),
        ("1990s", "1990-01-01", "1999-12-31"),
    ],
)
def test_decade_becomes_a_closed_date_range(decade, gte, lte):
    _, params = call(decade=decade)
    assert params["primaryReleaseDateGte"] == gte
    assert params["primaryReleaseDateLte"] == lte


def test_older_has_an_upper_bound_only():
    _, params = call(decade="older")
    assert params["primaryReleaseDateLte"] == "1989-12-31"
    assert "primaryReleaseDateGte" not in params


def test_decade_and_newest_intersect_to_the_tighter_bound():
    """'Newest films of the 1990s' — not a contradiction, and not a dropped filter."""
    _, params = call(sort="newest", decade="2010s")
    assert params["primaryReleaseDateGte"] == "2010-01-01"
    assert params["primaryReleaseDateLte"] == "2019-12-31"  # tighter than today


def test_decade_and_upcoming_intersect_on_the_lower_bound():
    _, params = call(sort="upcoming", decade="2020s")
    assert params["primaryReleaseDateGte"] == "2026-08-14"  # tighter than 2020-01-01
    assert params["primaryReleaseDateLte"] == "2029-12-31"


def test_trending_is_its_own_path_and_carries_only_a_page():
    path, params = call(sort="trending", media=None)
    assert path == "/discover/trending"
    assert params == {"page": 1}


@pytest.mark.parametrize(
    "overrides",
    [
        {"media": "movie"},
        {"genre": 27},
        {"decade": "2010s"},
        {"min_rating": 7},
    ],
)
def test_trending_with_any_filter_is_refused(overrides):
    """Upstream ignores or 400s these; refusing here keeps the answer honest."""
    with pytest.raises(browse.BrowseFilterConflict):
        call(sort="trending", **{"media": None, **overrides})


def test_page_is_carried_through():
    _, params = call(page=7)
    assert params["page"] == 7
