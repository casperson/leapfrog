"""Unit tests for matching VidAngel exports to Plex library titles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leapfrog import database as db
from leapfrog.vidangel_sidecars import (
    _build_episode_index,
    _build_movie_index,
    _load_raw_events,
    _parse_optional_int,
    generate_vidangel_sidecars,
    match_episode_catalog_row,
    match_movie_catalog_row,
)
from leapfrog.vidangel_export import get_vidangel_export_dir


class FakePlexClient:
    def __init__(self, episode_map: dict[str, tuple[str, int | None, int | None]]) -> None:
        self._episode_map = episode_map

    async def get_episode_match_info(self, rating_key: str) -> tuple[str, int | None, int | None]:
        return self._episode_map.get(rating_key, ("", None, None))


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


def test_match_movie_catalog_row_tolerates_non_numeric_year():
    row = {
        "media_id": "1",
        "title": "Angel Has Fallen",
        "slug": "angel-has-fallen",
        "extra_metadata": {"year": "not-a-year"},
    }
    index = _build_movie_index([row])

    match, detail = match_movie_catalog_row(
        {"title": "Angel Has Fallen", "year": "2019.0"},
        index,
    )

    assert match == row
    assert detail == "title-only movie match"


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


def test_build_episode_index_skips_non_numeric_episode_numbers():
    index = _build_episode_index(
        [
            {
                "media_id": "10",
                "media_type": "episode",
                "title": "Pilot",
                "slug": "sample-show-s1-e1",
                "season_number": "1.0",
                "episode_number": "immodesty_female",
                "extra_metadata": {"show_title": "Sample Show"},
            }
        ]
    )

    assert index == {}


def test_parse_optional_int_accepts_blank_and_float_like_values():
    assert _parse_optional_int("") is None
    assert _parse_optional_int("1") == 1
    assert _parse_optional_int("1.0") == 1
    assert _parse_optional_int("immodesty_female") is None


def test_load_raw_events_skips_malformed_rows(tmp_path: Path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "raw_filter_events.csv").write_text(
        "\n".join(
            [
                "media_id,media_type,title,slug,service_slug,season_number,episode_number,tag_set_id,path_keys,path_titles,display_title,description,tag_type,start_ms,end_ms,mapped_category,leaf_key",
                "1,movie,Angel Has Fallen,angel-has-fallen,netflix,immodesty_female,,41696,language/profanity/fuck,Language > Profanity > f-word,f-word,f-word,audio,12000,12000,profanity,fuck",
                "2,movie,Broken Row,broken-row,netflix,immodesty_female,,not-a-number,language/profanity/fuck,Language > Profanity > f-word,f-word,f-word,audio,12000,12000,profanity,fuck",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    grouped = _load_raw_events(export_dir)

    assert list(grouped.keys()) == [("movie", "1")]
    assert grouped[("movie", "1")][0].season_number is None


def test_load_raw_events_preserves_multiline_descriptions(tmp_path: Path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "raw_filter_events.csv").write_text(
        "\n".join(
            [
                "media_id,media_type,title,slug,service_slug,season_number,episode_number,tag_set_id,path_keys,path_titles,display_title,description,tag_type,start_ms,end_ms,mapped_category,leaf_key",
                '701978,episode,The Fourth of You Lie,the-fourth-of-you-lie,crunchyroll,1,4,1824000,sex_nudity_immodesty/immodesty_female,Nudity & Immodesty > Female Immodesty,Female Immodesty,"A teenage girl\'s shirt shows her cleavage',
                'and remains visible for a moment.",audiovisual,123000,126000,nudity,immodesty_female',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    grouped = _load_raw_events(export_dir)

    assert list(grouped.keys()) == [("episode", "701978")]
    assert grouped[("episode", "701978")][0].description == (
        "A teenage girl's shirt shows her cleavage\nand remains visible for a moment."
    )


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
    movie_path = tmp_path / "Angel Has Fallen (2019).mkv"
    movie_path.write_text("", encoding="utf-8")
    await db.upsert_scan_job(
        plex_guid="movie-guid",
        title="Angel Has Fallen",
        file_path=str(movie_path),
        rating_key="100",
        library_id="1",
        library_title="Movies",
        content_rating="R",
        media_type="movie",
        year=2019,
    )
    missing_movie_path = tmp_path / "Missing Movie (2024).mkv"
    missing_movie_path.write_text("", encoding="utf-8")
    await db.upsert_scan_job(
        plex_guid="missing-guid",
        title="Missing Movie",
        file_path=str(missing_movie_path),
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
    assert (export_dir / "matched_movie_plex_titles.csv").exists()
    assert (export_dir / "matched_tv_plex_titles.csv").exists()
    assert (export_dir / "unmatched_plex_titles.csv").exists()
    assert (export_dir / "unmatched_movie_plex_titles.csv").exists()
    assert (export_dir / "unmatched_tv_plex_titles.csv").exists()
    assert (export_dir / "ambiguous_plex_titles.csv").exists()
    assert (export_dir / "skipped_plex_titles.csv").exists()
    assert (export_dir / "out_plex_titles.csv").exists()
    assert (export_dir / "out_movie_plex_titles.csv").exists()
    assert (export_dir / "out_tv_plex_titles.csv").exists()
    unmatched_payload = json.loads((export_dir / "unmatched_plex_titles.json").read_text(encoding="utf-8"))
    assert unmatched_payload["count"] == 1
    out_payload = json.loads((export_dir / "out_plex_titles.json").read_text(encoding="utf-8"))
    assert out_payload["count"] == 1


@pytest.mark.asyncio
async def test_generate_vidangel_sidecars_skips_missing_media_paths(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db.set_db_path(data_dir / "leapfrog.db")
    await db.init_db()
    media_dir = tmp_path / "missing-library" / "Movie"
    missing_file = media_dir / "Missing Movie (2024).mkv"
    await db.upsert_scan_job(
        plex_guid="missing-file-guid",
        title="Angel Has Fallen",
        file_path=str(missing_file),
        rating_key="100",
        library_id="1",
        library_title="Movies",
        content_rating="R",
        media_type="movie",
        year=2019,
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

    result = await generate_vidangel_sidecars(export_dir=export_dir, dry_run=False)

    assert result["summary"]["skipped"] == 1
    assert result["summary"]["written"] == 0
    assert result["results"][0]["detail"] == "media directory does not exist"


@pytest.mark.asyncio
async def test_generate_vidangel_sidecars_collapses_fully_uncovered_tv_shows(monkeypatch, tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db.set_db_path(data_dir / "leapfrog.db")
    await db.init_db()

    uncovered_dir = tmp_path / "tv" / "No Filters Show" / "Season 1"
    uncovered_dir.mkdir(parents=True)
    uncovered_paths = [
        uncovered_dir / "No Filters Show - s01e01.mkv",
        uncovered_dir / "No Filters Show - s01e02.mkv",
    ]
    for index, media_path in enumerate(uncovered_paths, start=1):
        media_path.write_text("", encoding="utf-8")
        await db.upsert_scan_job(
            plex_guid=f"show-no-filters-ep{index}",
            title=f"Episode {index}",
            file_path=str(media_path),
            rating_key=f"200{index}",
            library_id="2",
            library_title="TV",
            content_rating="TV-14",
            media_type="episode",
            show_guid="show-no-filters",
        )

    partial_dir = tmp_path / "tv" / "Partial Filters Show" / "Season 1"
    partial_dir.mkdir(parents=True)
    partial_paths = [
        partial_dir / "Partial Filters Show - s01e01.mkv",
        partial_dir / "Partial Filters Show - s01e02.mkv",
    ]
    for index, media_path in enumerate(partial_paths, start=1):
        media_path.write_text("", encoding="utf-8")
        await db.upsert_scan_job(
            plex_guid=f"show-partial-ep{index}",
            title=f"Partial Episode {index}",
            file_path=str(media_path),
            rating_key=f"300{index}",
            library_id="2",
            library_title="TV",
            content_rating="TV-14",
            media_type="episode",
            show_guid="show-partial",
        )

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "movies_catalog.json").write_text(json.dumps({"titles": []}), encoding="utf-8")
    (export_dir / "tv_catalog.json").write_text(
        json.dumps(
            {
                "titles": [
                    {
                        "media_id": "tv-1",
                        "media_type": "episode",
                        "title": "Partial Episode 1",
                        "slug": "partial-filters-show-s1-e1",
                        "season_number": 1,
                        "episode_number": 1,
                        "extra_metadata": {"show_title": "Partial Filters Show"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (export_dir / "raw_filter_events.csv").write_text(
        "\n".join(
            [
                "media_id,media_type,title,slug,service_slug,season_number,episode_number,tag_set_id,path_keys,path_titles,display_title,description,tag_type,start_ms,end_ms,mapped_category,leaf_key",
                "tv-1,episode,Partial Episode 1,partial-filters-show-s1-e1,netflix,1,1,41696,language/profanity/fuck,Language > Profanity > f-word,f-word,f-word,audio,12000,12000,profanity,fuck",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "leapfrog.vidangel_sidecars.plex_mod.get_client",
        lambda: FakePlexClient(
            {
                "2001": ("No Filters Show", 1, 1),
                "2002": ("No Filters Show", 1, 2),
                "3001": ("Partial Filters Show", 1, 1),
                "3002": ("Partial Filters Show", 1, 2),
            }
        ),
    )

    result = await generate_vidangel_sidecars(export_dir=export_dir, dry_run=True)

    assert result["summary"]["matched"] == 1
    assert result["summary"]["unmatched"] == 3

    unmatched_tv_payload = json.loads((export_dir / "unmatched_tv_plex_titles.json").read_text(encoding="utf-8"))
    assert unmatched_tv_payload["count"] == 2
    assert {row["title"] for row in unmatched_tv_payload["titles"]} == {
        "No Filters Show",
        "Partial Episode 2",
    }
    collapsed_row = next(row for row in unmatched_tv_payload["titles"] if row["title"] == "No Filters Show")
    assert collapsed_row["media_type"] == "show"
    assert collapsed_row["detail"] == "no VidAngel filters for show"

    out_tv_payload = json.loads((export_dir / "out_tv_plex_titles.json").read_text(encoding="utf-8"))
    assert out_tv_payload["count"] == 2
