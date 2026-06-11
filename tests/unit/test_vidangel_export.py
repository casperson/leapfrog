"""Unit tests for the VidAngel export helpers."""

from __future__ import annotations

import subprocess
import sys
import zipfile
import json
from pathlib import Path

import pytest

from leapfrog.vidangel_export import (
    DEFAULT_MIN_SEGMENT_MS,
    ShowSummary,
    TitleArtifact,
    VidAngelExportDataError,
    _create_simple_xlsx,
    build_vidangel_filter_catalog,
    _parse_optional_int,
    _parse_required_int,
    _show_catalog_rows,
    generate_filtered_vidangel_skp,
    WorkStub,
    build_catalog_queries,
    build_show_summaries,
    build_sidecar_payload,
    flatten_tag_tree,
)


def test_build_catalog_queries_includes_base_service_and_category_fanout():
    parameters = {
        "orders_by": {
            "options": [
                {"value": "-popularity"},
                {"value": "-published_at"},
                {"value": "title"},
                {"value": "shuffle"},
            ]
        },
        "services": {
            "options": [
                {"catalogs": [{"id": 3}, {"id": 12}]},
                {"catalogs": [{"id": 13}]},
            ]
        },
        "categories": {
            "options": [
                {"slug": "action-and-adventure"},
                {"slug": "drama"},
            ]
        },
    }

    queries = build_catalog_queries(parameters)
    labels = {query.label for query in queries}

    assert "works:all:-popularity" in labels
    assert "works:movie:title" in labels
    assert "catalog:3:movie" in labels
    assert "catalog:13:show" in labels
    assert "genre:action-and-adventure:movie" in labels
    assert "genre:drama:show" in labels
    assert all("shuffle" not in query.label for query in queries)


def test_parse_int_helpers_tolerate_float_like_and_invalid_values():
    assert _parse_optional_int("1.0") == 1
    assert _parse_optional_int("immodesty_female") is None
    assert _parse_required_int("2.0", field_name="episode.id") == 2


def test_work_stub_from_payload_tolerates_non_numeric_year_and_tag_count():
    work = WorkStub.from_payload(
        {
            "id": "42",
            "slug": "sample-title",
            "title": "Sample Title",
            "type": "movie",
            "year": "not-a-year",
            "tag_count": "not-a-count",
        }
    )

    assert work.id == 42
    assert work.year is None
    assert work.tag_count == 0


def test_flatten_tag_tree_collects_exact_events_and_leaf_definitions():
    tag_tree = {
        "tag_categories": [
            {
                "id": 1,
                "key": "language",
                "display_title": "Language",
                "default_type": "audio",
                "child_categories": [
                    {
                        "id": 18,
                        "key": "profanity",
                        "display_title": "Profanity",
                        "default_type": "audio",
                        "child_categories": [
                            {
                                "id": 61,
                                "key": "fuck",
                                "display_title": "f-word",
                                "default_type": "audio",
                                "tags": [
                                    {
                                        "description": "f-word",
                                        "type": "audio",
                                        "start_approx": 12,
                                        "end_approx": 12,
                                    }
                                ],
                                "child_categories": [],
                            }
                        ],
                        "tags": [],
                    }
                ],
                "tags": [],
            },
            {
                "id": 2,
                "key": "sex_nudity_immodesty",
                "display_title": "Nudity & Immodesty",
                "default_type": "audiovisual",
                "child_categories": [
                    {
                        "id": 46,
                        "key": "immodesty_female",
                        "display_title": "Female Immodesty",
                        "default_type": "audiovisual",
                        "tags": [
                            {
                                "description": "A woman is seen in a bra and panties.",
                                "type": "audiovisual",
                                "start_approx": 20,
                                "end_approx": 24,
                            }
                        ],
                        "child_categories": [],
                    }
                ],
                "tags": [],
            },
        ]
    }
    media = {
        "media_id": "701254",
        "media_type": "episode",
        "title": "S1:E1",
        "slug": "show-s1-e1",
        "service_slug": "appletv",
        "season_number": 1,
        "episode_number": 1,
        "tag_set_id": 68845,
    }

    events, definitions = flatten_tag_tree(tag_tree, media=media)

    assert len(events) == 2
    assert events[0].start_ms == 12_000
    assert events[0].mapped_category == "language_profanity"
    assert events[1].mapped_category == "sex_nudity_immodesty"
    assert definitions["fuck"].path_keys == ("language", "profanity", "fuck")
    assert definitions["immodesty_female"].example_description == "A woman is seen in a bra and panties."


def test_flatten_tag_tree_tolerates_non_numeric_timings():
    tag_tree = {
        "tag_categories": [
            {
                "id": "1",
                "key": "language",
                "display_title": "Language",
                "default_type": "audio",
                "tags": [
                    {
                        "description": "f-word",
                        "type": "audio",
                        "start_approx": "immodesty_female",
                        "end_approx": "12.0",
                    }
                ],
                "child_categories": [],
            }
        ]
    }
    media = {
        "media_id": "701254",
        "media_type": "episode",
        "title": "S1:E1",
        "slug": "show-s1-e1",
        "service_slug": "appletv",
        "season_number": 1,
        "episode_number": 1,
        "tag_set_id": "68845",
    }

    events, definitions = flatten_tag_tree(tag_tree, media=media)

    assert len(events) == 1
    assert events[0].start_ms == 0
    assert events[0].end_ms == 12_000
    assert definitions["language"].category_id == 1


def test_build_sidecar_payload_merges_close_events_and_expands_zero_length_tags():
    tag_tree = {
        "tag_categories": [
            {
                "id": 1,
                "key": "language",
                "display_title": "Language",
                "default_type": "audio",
                "child_categories": [
                    {
                        "id": 18,
                        "key": "profanity",
                        "display_title": "Profanity",
                        "default_type": "audio",
                        "child_categories": [
                            {
                                "id": 61,
                                "key": "fuck",
                                "display_title": "f-word",
                                "default_type": "audio",
                                "tags": [
                                    {
                                        "description": "f-word",
                                        "type": "audio",
                                        "start_approx": 10,
                                        "end_approx": 10,
                                    },
                                    {
                                        "description": "f-word",
                                        "type": "audio",
                                        "start_approx": 11,
                                        "end_approx": 11,
                                    },
                                ],
                                "child_categories": [],
                            }
                        ],
                        "tags": [],
                    }
                ],
                "tags": [],
            }
        ]
    }
    media = {
        "media_id": "675075",
        "media_type": "movie",
        "title": "Angel Has Fallen",
        "slug": "angel-has-fallen",
        "service_slug": "netflix",
        "season_number": None,
        "episode_number": None,
        "tag_set_id": 41695,
    }

    events, _ = flatten_tag_tree(tag_tree, media=media)
    payload = build_sidecar_payload(
        media_id="675075",
        title="Angel Has Fallen",
        slug="angel-has-fallen",
        events=events,
    )

    assert payload["format"] == "leapfrog.segment.sidecar/v1"
    assert len(payload["segments"]) == 1
    segment = payload["segments"][0]
    assert segment["category"] == "language_profanity"
    assert segment["end_time"] - segment["start_time"] >= DEFAULT_MIN_SEGMENT_MS / 1000
    assert "fuck" in segment["labels"]


def test_title_artifact_summary_record_is_compact():
    artifact = TitleArtifact(
        media_id="675075",
        media_type="movie",
        title="Angel Has Fallen",
        slug="angel-has-fallen",
        service_slug="netflix",
        season_number=None,
        episode_number=None,
        summary={"tag_set_id": 41696},
        events=(),
        sidecar_payload={"format": "leapfrog.segment.sidecar/v1", "segments": []},
        extra_metadata={"year": 2019, "rating": "R"},
    )

    record = artifact.summary_record()

    assert record["media_id"] == "675075"
    assert record["event_count"] == 0
    assert record["tag_set_id"] == 41696
    assert "summary" in record
    assert "extra_metadata" in record


def test_title_artifact_summary_record_preserves_episode_shape():
    artifact = TitleArtifact(
        media_id="701254",
        media_type="episode",
        title="Pilot",
        slug="sample-show-s1-e1",
        service_slug="appletv",
        season_number=1,
        episode_number=1,
        summary={"tag_set_id": 68845},
        events=(),
        sidecar_payload={"format": "leapfrog.segment.sidecar/v1", "segments": []},
        extra_metadata={"show_title": "Sample Show", "show_slug": "sample-show"},
    )

    record = artifact.summary_record()

    assert record["media_type"] == "episode"
    assert record["season_number"] == 1
    assert record["episode_number"] == 1
    assert record["tag_set_id"] == 68845


def test_build_show_summaries_rolls_up_episode_artifacts():
    first = TitleArtifact(
        media_id="701254",
        media_type="episode",
        title="Pilot",
        slug="sample-show-s1-e1",
        service_slug="appletv",
        season_number=1,
        episode_number=1,
        summary={"tag_set_id": 68845},
        events=(),
        sidecar_payload={"format": "leapfrog.segment.sidecar/v1", "segments": []},
        extra_metadata={"show_title": "Sample Show", "show_slug": "sample-show", "year": 2024, "rating": "TV-14"},
    )
    second = TitleArtifact(
        media_id="701255",
        media_type="episode",
        title="Episode 2",
        slug="sample-show-s1-e2",
        service_slug="appletv",
        season_number=1,
        episode_number=2,
        summary={"tag_set_id": 68846},
        events=(),
        sidecar_payload={"format": "leapfrog.segment.sidecar/v1", "segments": []},
        extra_metadata={"show_title": "Sample Show", "show_slug": "sample-show", "year": 2024, "rating": "TV-14"},
    )

    summaries = build_show_summaries([first, second])

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.show_title == "Sample Show"
    assert summary.episode_count == 2
    assert summary.season_count == 1
    assert summary.tag_set_count == 2


def test_build_show_summaries_tolerates_invalid_year_and_tag_set_id():
    artifact = TitleArtifact(
        media_id="701254",
        media_type="episode",
        title="Pilot",
        slug="sample-show-s1-e1",
        service_slug="appletv",
        season_number=1,
        episode_number=1,
        summary={"tag_set_id": "bad-tag-set"},
        events=(),
        sidecar_payload={"format": "leapfrog.segment.sidecar/v1", "segments": []},
        extra_metadata={"show_title": "Sample Show", "show_slug": "sample-show", "year": "unknown"},
    )

    summary = build_show_summaries([artifact])[0]

    assert summary.year is None
    assert summary.tag_set_count == 0


def test_show_catalog_rows_returns_expected_columns():
    rows = _show_catalog_rows(
        [
            ShowSummary(
                show_title="Sample Show",
                show_slug="sample-show",
                service_slug="appletv",
                year=2024,
                rating="TV-14",
                season_count=2,
                episode_count=16,
                event_count=120,
                category_count=3,
                tag_set_count=16,
            )
        ]
    )

    assert rows == [
        {
            "show_title": "Sample Show",
            "show_slug": "sample-show",
            "service_slug": "appletv",
            "year": 2024,
            "rating": "TV-14",
            "season_count": 2,
            "episode_count": 16,
            "event_count": 120,
            "category_count": 3,
            "tag_set_count": 16,
        }
    ]


def test_create_simple_xlsx_writes_expected_tabs(tmp_path: Path):
    path = tmp_path / "vidangel_catalog.xlsx"
    _create_simple_xlsx(
        path,
        {
            "movies_catalog": [{"title": "Movie A", "event_count": 4}],
            "tv_catalog": [{"title": "Episode A", "event_count": 7}],
            "shows_catalog": [{"show_title": "Show A", "episode_count": 10}],
        },
    )

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")

    assert "[Content_Types].xml" in names
    assert "xl/workbook.xml" in names
    assert "xl/worksheets/sheet1.xml" in names
    assert "xl/worksheets/sheet2.xml" in names
    assert "xl/worksheets/sheet3.xml" in names
    assert 'name="movies_catalog"' in workbook_xml
    assert 'name="tv_catalog"' in workbook_xml
    assert 'name="shows_catalog"' in workbook_xml


def test_module_help_entrypoint_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "leapfrog.vidangel_export", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "output-dir" in result.stdout
    assert "write-per-title-artifacts" in result.stdout
    assert "export-skp" in result.stdout


def _write_sample_vidangel_export(tmp_path: Path) -> Path:
    export_dir = tmp_path / "vidangel"
    export_dir.mkdir()
    (export_dir / "title_catalog.json").write_text(
        json.dumps(
            {
                "titles": [
                    {
                        "media_id": "movie-1",
                        "media_type": "movie",
                        "title": "Sample Movie",
                        "slug": "sample-movie",
                        "event_count": 3,
                        "category_count": 2,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (export_dir / "tag_definitions.json").write_text(
        json.dumps(
            {
                "definitions": [
                    {
                        "key": "fuck",
                        "display_title": "Fu**",
                        "path_keys": ["language", "profanity", "fuck"],
                        "path_titles": ["Language", "Profanity", "f-word"],
                        "default_type": "audio",
                        "mapped_category": "language_profanity",
                        "example_description": "Example f-word usage.",
                    },
                    {
                        "key": "shit",
                        "display_title": "Sh**",
                        "path_keys": ["language", "profanity", "shit"],
                        "path_titles": ["Language", "Profanity", "s-word"],
                        "default_type": "audio",
                        "mapped_category": "language_profanity",
                        "example_description": "Example s-word usage.",
                    },
                    {
                        "key": "immodesty_female",
                        "display_title": "Female Immodesty",
                        "path_keys": ["sex_nudity_immodesty", "immodesty_female"],
                        "path_titles": ["Nudity & Immodesty", "Female Immodesty"],
                        "default_type": "audiovisual",
                        "mapped_category": "sex_nudity_immodesty",
                        "example_description": "Example immodesty filter.",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (export_dir / "raw_filter_events.csv").write_text(
        "\n".join(
            [
                "media_id,media_type,title,slug,service_slug,season_number,episode_number,tag_set_id,path_keys,path_titles,display_title,description,tag_type,start_ms,end_ms,mapped_category,leaf_key",
                "movie-1,movie,Sample Movie,sample-movie,vidangel,,,1,language/profanity/fuck,Language > Profanity > f-word,f-word,f-word,audio,1000,1000,language_profanity,fuck",
                "movie-1,movie,Sample Movie,sample-movie,vidangel,,,1,language/profanity/fuck,Language > Profanity > f-word,f-word,f-word,audio,1300,1300,language_profanity,fuck",
                "movie-1,movie,Sample Movie,sample-movie,vidangel,,,1,language/profanity/shit,Language > Profanity > s-word,s-word,s-word,audio,4000,4000,language_profanity,shit",
                "movie-1,movie,Sample Movie,sample-movie,vidangel,,,1,sex_nudity_immodesty/immodesty_female,Nudity & Immodesty > Female Immodesty,Female Immodesty,Example immodesty filter.,audiovisual,6000,6000,sex_nudity_immodesty,immodesty_female",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return export_dir


def _write_sample_vidangel_export_without_raw_events(tmp_path: Path) -> Path:
    export_dir = _write_sample_vidangel_export(tmp_path)
    (export_dir / "raw_filter_events.csv").unlink()
    return export_dir


@pytest.mark.asyncio
async def test_build_vidangel_filter_catalog_groups_leaf_filters(tmp_path: Path):
    export_dir = _write_sample_vidangel_export(tmp_path)

    catalog = await build_vidangel_filter_catalog("movie-1", export_dir=export_dir)

    assert catalog["title"]["title"] == "Sample Movie"
    assert catalog["leaf_count"] == 3
    assert [category["label"] for category in catalog["categories"]] == [
        "Nudity & Immodesty",
        "Profanity",
    ]
    profanity = next(category for category in catalog["categories"] if category["key"] == "language_profanity")
    assert [filter_row["leaf_key"] for filter_row in profanity["filters"]] == ["fuck", "shit"]


@pytest.mark.asyncio
async def test_build_vidangel_filter_catalog_rejects_missing_raw_events(tmp_path: Path):
    export_dir = _write_sample_vidangel_export_without_raw_events(tmp_path)

    with pytest.raises(VidAngelExportDataError, match="raw filter events export is missing"):
        await build_vidangel_filter_catalog("movie-1", export_dir=export_dir)


@pytest.mark.asyncio
async def test_generate_filtered_vidangel_skp_uses_only_selected_leaf_keys(tmp_path: Path):
    export_dir = _write_sample_vidangel_export(tmp_path)
    output_path = tmp_path / "sample_movie.skp"

    result = await generate_filtered_vidangel_skp(
        "movie-1",
        ["fuck", "immodesty_female"],
        export_dir=export_dir,
        output_path=output_path,
    )

    assert result["selected_event_count"] == 3
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "Example immodesty filter." in content
    assert "s-word" not in content


@pytest.mark.asyncio
async def test_generate_filtered_vidangel_skp_rejects_missing_raw_events(tmp_path: Path):
    export_dir = _write_sample_vidangel_export_without_raw_events(tmp_path)

    with pytest.raises(VidAngelExportDataError, match="raw filter events export is missing"):
        await generate_filtered_vidangel_skp("movie-1", ["fuck"], export_dir=export_dir)


@pytest.mark.asyncio
async def test_generate_filtered_vidangel_skp_rejects_empty_and_unknown_leaf_keys(tmp_path: Path):
    export_dir = _write_sample_vidangel_export(tmp_path)

    with pytest.raises(ValueError):
        await generate_filtered_vidangel_skp("movie-1", [], export_dir=export_dir)

    with pytest.raises(ValueError):
        await generate_filtered_vidangel_skp("movie-1", ["unknown"], export_dir=export_dir)
