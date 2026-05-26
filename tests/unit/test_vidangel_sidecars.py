"""Unit tests for matching VidAngel exports to Plex library titles."""

from __future__ import annotations

from leapfrog.vidangel_sidecars import (
    _build_episode_index,
    _build_movie_index,
    match_episode_catalog_row,
    match_movie_catalog_row,
)


def test_match_movie_catalog_row_uses_exact_title_and_year():
    row = {
        "media_id": "1",
        "title": "Angel Has Fallen",
        "slug": "angel-has-fallen",
        "extra_metadata": {"year": 2019},
    }
    index = _build_movie_index([row])

    match, detail = match_movie_catalog_row(
        {"title": "Angel Has Fallen", "year": 2019},
        index,
    )

    assert match == row
    assert detail == "exact title/year match"


def test_match_movie_catalog_row_reports_ambiguous_title_only_match():
    first = {
        "media_id": "1",
        "title": "The Thing",
        "slug": "the-thing-1982",
        "extra_metadata": {"year": 1982},
    }
    second = {
        "media_id": "2",
        "title": "The Thing",
        "slug": "the-thing-2011",
        "extra_metadata": {"year": 2011},
    }
    index = _build_movie_index([first, second])

    match, detail = match_movie_catalog_row(
        {"title": "The Thing", "year": None},
        index,
    )

    assert match is None
    assert detail == "ambiguous movie title match"


def test_match_episode_catalog_row_uses_show_and_numbers():
    row = {
        "media_id": "10",
        "media_type": "episode",
        "title": "Pilot",
        "slug": "sample-show-s1-e1",
        "season_number": 1,
        "episode_number": 1,
        "extra_metadata": {"show_title": "Sample Show"},
    }
    index = _build_episode_index([row])

    match, detail = match_episode_catalog_row(
        show_title="Sample Show",
        season_number=1,
        episode_number=1,
        episode_index=index,
    )

    assert match == row
    assert detail == "exact show/season/episode match"
