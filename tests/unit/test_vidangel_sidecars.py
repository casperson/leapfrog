"""Unit tests for matching VidAngel exports to Plex library titles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leapfrog import database as db
from leapfrog.vidangel_sidecars import (
    _build_episode_index,
    _build_movie_index,
    _parse_optional_int,
    generate_vidangel_sidecars,
    match_episode_catalog_row,
    match_movie_catalog_row,
)
from leapfrog.vidangel_export import get_vidangel_export_dir


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


def test_parse_optional_int_accepts_blank_and_float_like_values():
    assert _parse_optional_int("") is None
    assert _parse_optional_int("1") == 1
    assert _parse_optional_int("1.0") == 1


def test_get_vidangel_export_dir_defaults_to_repo_vidangel():
    path = get_vidangel_export_dir()
    assert path.name == "vidangel"
    assert path.parent.name == "leapfrog"


@pytest.mark.asyncio
async def test_generate_vidangel_sidecars_writes_coverage_reports(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db.set_db_path(data_dir / "leapfrog.db")
    await db.init_db()
    await db.upsert_scan_job(
        plex_guid="movie-guid",
        title="Angel Has Fallen",
        file_path=str(tmp_path / "Angel Has Fallen (2019).mkv"),
        rating_key="100",
        library_id="1",
        library_title="Movies",
        content_rating="R",
        media_type="movie",
        year=2019,
    )
    await db.upsert_scan_job(
        plex_guid="missing-guid",
        title="Missing Movie",
        file_path=str(tmp_path / "Missing Movie (2024).mkv"),
        rating_key="101",
        library_id="1",
        library_title="Movies",
        content_rating="PG-13",
        media_type="movie",
        year=2024,
    )

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "movies_catalog.json").write_text(
        json.dumps(
            {
                "titles": [
                    {
                        "media_id": "1",
                        "media_type": "movie",
                        "title": "Angel Has Fallen",
                        "slug": "angel-has-fallen",
                        "extra_metadata": {"year": 2019},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (export_dir / "tv_catalog.json").write_text(json.dumps({"titles": []}), encoding="utf-8")
    (export_dir / "raw_filter_events.csv").write_text(
        "\n".join(
            [
                "media_id,media_type,title,slug,service_slug,season_number,episode_number,tag_set_id,path_keys,path_titles,display_title,description,tag_type,start_ms,end_ms,mapped_category,leaf_key",
                "1,movie,Angel Has Fallen,angel-has-fallen,netflix,,,41696,language/profanity/fuck,Language > Profanity > f-word,f-word,f-word,audio,12000,12000,profanity,fuck",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = await generate_vidangel_sidecars(export_dir=export_dir, dry_run=True)

    assert result["summary"]["matched"] == 1
    assert result["summary"]["unmatched"] == 1
    assert (export_dir / "matched_plex_titles.csv").exists()
    assert (export_dir / "unmatched_plex_titles.csv").exists()
    assert (export_dir / "ambiguous_plex_titles.csv").exists()
    assert (export_dir / "skipped_plex_titles.csv").exists()
    unmatched_payload = json.loads((export_dir / "unmatched_plex_titles.json").read_text(encoding="utf-8"))
    assert unmatched_payload["count"] == 1
