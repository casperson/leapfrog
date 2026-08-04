"""Unit tests for the VidAngel export helpers."""

from __future__ import annotations

import subprocess
import sys
import zipfile
import json
from dataclasses import replace
from pathlib import Path

import pytest

import leapfrog.vidangel_export as vidangel_export
from leapfrog.vidangel_export import (
    DEFAULT_MIN_SEGMENT_MS,
    ShowSummary,
    TitleArtifact,
    VidAngelExportDataError,
    _format_clean_media_player_time,
    _create_simple_xlsx,
    build_vidangel_filter_catalog,
    _parse_optional_int,
    _parse_required_int,
    _show_catalog_rows,
    generate_filtered_vidangel_skp,
    load_vidangel_exportable_title_catalog_records,
    load_vidangel_raw_events_for_title,
    prepare_vidangel_export,
    WorkStub,
    build_catalog_queries,
    build_clean_media_player_skp_payload,
    build_show_summaries,
    build_sidecar_payload,
    flatten_tag_tree,
)
from leapfrog.vidangel_taxonomy import (
    vidangel_ui_category_family,
    vidangel_ui_category_family_sort_key,
    vidangel_ui_subcategory_sort_key,
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


def test_vidangel_ui_category_order_groups_related_filters():
    subcategories = [
        "credits",
        "sex_nudity_immodesty",
        "alcohol_or_drug_use",
        "language_profanity",
        "violence_blood_gore",
        "sex_any",
        "kissing",
        "language_language_sexual",
    ]

    assert sorted(subcategories, key=vidangel_ui_subcategory_sort_key) == [
        "language_profanity",
        "sex_any",
        "sex_nudity_immodesty",
        "kissing",
        "language_language_sexual",
        "violence_blood_gore",
        "alcohol_or_drug_use",
        "credits",
    ]
    category_families = [vidangel_ui_category_family(category) for category in subcategories]
    assert sorted(set(category_families), key=vidangel_ui_category_family_sort_key) == [
        "profanity",
        "sexual_content",
        "violence",
        "drugs_alcohol",
        "other",
    ]


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
                                        "start_approx": "12.25",
                                        "end_approx": "12.45",
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
    assert events[0].start_ms == 12_250
    assert events[0].end_ms == 12_450
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
    assert "prepare" in result.stdout


def test_export_defaults_to_configured_vidangel_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LEAPFROG_VIDANGEL_EXPORT_DIR", str(tmp_path))

    args = vidangel_export._build_arg_parser().parse_args([])

    assert args.output_dir == str(tmp_path)


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
                        "mapped_category": "profanity",
                        "example_description": "Example f-word usage.",
                    },
                    {
                        "key": "shit",
                        "display_title": "Sh**",
                        "path_keys": ["language", "profanity", "shit"],
                        "path_titles": ["Language", "Profanity", "s-word"],
                        "default_type": "audio",
                        "mapped_category": "profanity",
                        "example_description": "Example s-word usage.",
                    },
                    {
                        "key": "immodesty_female",
                        "display_title": "Female Immodesty",
                        "path_keys": ["sex_nudity_immodesty", "immodesty_female"],
                        "path_titles": ["Nudity & Immodesty", "Female Immodesty"],
                        "default_type": "audiovisual",
                        "mapped_category": "sexual_content",
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
async def test_prepare_vidangel_export_builds_index_without_network(tmp_path: Path):
    export_dir = _write_sample_vidangel_export(tmp_path)

    result = await prepare_vidangel_export(export_dir)

    assert result["ok"] is True
    assert result["catalog_title_count"] == 1
    assert result["indexed_title_count"] == 1
    assert result["event_count"] == 4
    assert result["definition_count"] == 3
    assert Path(result["index_path"]).exists()


@pytest.mark.asyncio
async def test_prepare_vidangel_export_caches_catalog_and_definitions(tmp_path: Path, monkeypatch):
    export_dir = _write_sample_vidangel_export(tmp_path)
    original_reader = vidangel_export._read_json_rows_sync
    read_keys: list[str] = []

    def counted_reader(source: Path, rows_key: str):
        read_keys.append(rows_key)
        return original_reader(source, rows_key)

    monkeypatch.setattr(vidangel_export, "_read_json_rows_sync", counted_reader)

    await prepare_vidangel_export(export_dir)
    await prepare_vidangel_export(export_dir)

    assert read_keys.count("titles") == 1
    assert read_keys.count("definitions") == 1


@pytest.mark.asyncio
async def test_build_vidangel_filter_catalog_groups_leaf_filters(tmp_path: Path):
    export_dir = _write_sample_vidangel_export(tmp_path)

    catalog = await build_vidangel_filter_catalog("movie-1", export_dir=export_dir)

    assert catalog["title"]["title"] == "Sample Movie"
    assert catalog["leaf_count"] == 3
    assert [category["label"] for category in catalog["categories"]] == [
        "Profanity",
        "Sexual Content",
    ]
    profanity = next(category for category in catalog["categories"] if category["key"] == "profanity")
    assert [filter_row["leaf_key"] for filter_row in profanity["filters"]] == ["fuck", "shit"]
    assert profanity["filters"][0]["subcategory"] == "language_profanity"
    event_ids = [event["event_id"] for event in profanity["filters"][0]["events"]]
    assert all(event_id.startswith("event-") for event_id in event_ids)
    assert len(set(event_ids)) == 2
    assert profanity["filters"][0]["events"][0]["description"] == "f-word"
    assert (export_dir / "raw_filter_events.index.json").exists()


@pytest.mark.asyncio
async def test_filter_catalog_event_ids_survive_raw_row_reordering(tmp_path: Path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_export = _write_sample_vidangel_export(first_root)
    second_export = _write_sample_vidangel_export(second_root)
    source = second_export / "raw_filter_events.csv"
    lines = source.read_text(encoding="utf-8").splitlines()
    source.write_text("\n".join([lines[0], *reversed(lines[1:])]) + "\n", encoding="utf-8")

    first_catalog = await build_vidangel_filter_catalog("movie-1", export_dir=first_export)
    second_catalog = await build_vidangel_filter_catalog("movie-1", export_dir=second_export)

    def ids_by_start(catalog: dict) -> dict[int, str]:
        return {
            event["start_ms"]: event["event_id"]
            for category in catalog["categories"]
            for filter_row in category["filters"]
            for event in filter_row["events"]
        }

    assert ids_by_start(first_catalog) == ids_by_start(second_catalog)


@pytest.mark.asyncio
async def test_load_exportable_catalog_excludes_titles_without_events(tmp_path: Path):
    export_dir = _write_sample_vidangel_export(tmp_path)
    payload = json.loads((export_dir / "title_catalog.json").read_text(encoding="utf-8"))
    payload["titles"].append(
        {
            "media_id": "movie-empty",
            "media_type": "movie",
            "title": "Empty Movie",
            "event_count": 0,
        }
    )
    (export_dir / "title_catalog.json").write_text(json.dumps(payload), encoding="utf-8")

    titles = await load_vidangel_exportable_title_catalog_records(export_dir)

    assert [title["media_id"] for title in titles] == ["movie-1"]


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
    raw_skp = output_path.read_text(encoding="utf-8")
    payload = json.loads(raw_skp)
    assert raw_skp.startswith('{"SceneFileTypeId":1,"Year":null')
    assert not raw_skp.endswith("\n")
    assert list(payload) == [
        "SceneFileTypeId",
        "Year",
        "SeasonNumber",
        "EpisodeNumber",
        "SkipScenes",
        "SkipScenesSyncs",
        "Unsynced",
    ]
    assert payload["SceneFileTypeId"] == 1
    assert payload["SkipScenesSyncs"] == []
    assert payload["Unsynced"] is False
    assert [scene["SceneType"] for scene in payload["SkipScenes"]] == [
        "Profanity",
        "Nudity",
    ]
    assert payload["SkipScenes"][0]["StartTime"] == "00:00:01"
    assert payload["SkipScenes"][0]["EndTime"] == "00:00:01.8000000"


@pytest.mark.asyncio
async def test_generate_filtered_vidangel_skp_uses_only_selected_event_ids(tmp_path: Path):
    export_dir = _write_sample_vidangel_export(tmp_path)
    catalog = await build_vidangel_filter_catalog("movie-1", export_dir=export_dir)
    event_ids_by_start = {
        event["start_ms"]: event["event_id"]
        for category in catalog["categories"]
        for filter_row in category["filters"]
        for event in filter_row["events"]
    }
    selected_event_ids = [event_ids_by_start[1300], event_ids_by_start[6000]]

    result = await generate_filtered_vidangel_skp(
        "movie-1",
        export_dir=export_dir,
        selected_event_ids=selected_event_ids,
    )

    assert result["selected_event_ids"] == selected_event_ids
    assert result["selected_event_count"] == 2
    payload = json.loads(result["skp_text"])
    assert [scene["StartTime"] for scene in payload["SkipScenes"]] == [
        "00:00:01.3000000",
        "00:00:06",
    ]
    assert [scene["SceneType"] for scene in payload["SkipScenes"]] == [
        "Profanity",
        "Nudity",
    ]


@pytest.mark.asyncio
async def test_generate_exact_event_skp_preserves_only_selected_event_record(tmp_path: Path):
    export_dir = _write_sample_vidangel_export(tmp_path)
    catalog = await build_vidangel_filter_catalog("movie-1", export_dir=export_dir)
    first_event = next(
        event
        for category in catalog["categories"]
        for filter_row in category["filters"]
        for event in filter_row["events"]
        if event["start_ms"] == 1000
    )

    result = await generate_filtered_vidangel_skp(
        "movie-1",
        export_dir=export_dir,
        selected_event_ids=[first_event["event_id"]],
    )

    payload = json.loads(result["skp_text"])
    assert payload["SkipScenes"] == [
        {
            "Id": 1,
            "SceneType": "Profanity",
            "StartTime": "00:00:01",
            "EndTime": "00:00:01.5000000",
            "Blur": False,
        }
    ]


def test_clean_media_player_skp_preserves_short_event_and_uses_fixed_width_hours():
    event = vidangel_export.RawFilterEvent(
        media_id="episode-1",
        media_type="episode",
        title="Episode",
        slug="episode",
        service_slug="vidangel",
        tag_set_id=1,
        season_number=2,
        episode_number=3,
        path_keys=("sex_nudity_immodesty", "immodesty_female"),
        path_titles=("Nudity & Immodesty", "Female Immodesty"),
        display_title="Female Immodesty",
        description=None,
        tag_type="audiovisual",
        start_ms=7_386_500,
        end_ms=7_386_700,
        mapped_category="sex_nudity_immodesty",
        leaf_key="immodesty_female",
    )

    payload = build_clean_media_player_skp_payload(
        {"year": 2024, "season_number": 2, "episode_number": 3},
        [event],
    )

    assert _format_clean_media_player_time(0) == "00:00:00"
    assert _format_clean_media_player_time(7_386_500) == "02:03:06.5000000"
    assert payload["Year"] == 2024
    assert payload["SeasonNumber"] == 2
    assert payload["EpisodeNumber"] == 3
    assert payload["SkipScenes"][0]["StartTime"] == "02:03:06.5000000"
    assert payload["SkipScenes"][0]["EndTime"] == "02:03:06.7000000"


def test_clean_media_player_skp_consolidates_overlapping_and_touching_events():
    base_event = vidangel_export.RawFilterEvent(
        media_id="movie-1",
        media_type="movie",
        title="Movie",
        slug="movie",
        service_slug="vidangel",
        tag_set_id=1,
        season_number=None,
        episode_number=None,
        path_keys=("language", "profanity", "fuck"),
        path_titles=("Language", "Profanity", "f-word"),
        display_title="f-word",
        description=None,
        tag_type="audio",
        start_ms=10_000,
        end_ms=12_000,
        mapped_category="language_profanity",
        leaf_key="fuck",
    )
    events = [
        base_event,
        replace(base_event, start_ms=10_000, end_ms=12_000, leaf_key="shit"),
        replace(base_event, start_ms=11_000, end_ms=15_000, mapped_category="violence_blood_gore", leaf_key="graphic"),
        replace(base_event, start_ms=15_000, end_ms=16_000, mapped_category="alcohol_or_drug_use", leaf_key="drugs_illegal"),
        replace(base_event, start_ms=20_000, end_ms=21_000, mapped_category="sex_nudity_immodesty", leaf_key="immodesty_female"),
    ]

    payload = build_clean_media_player_skp_payload({}, events)

    assert payload["SkipScenes"] == [
        {
            "Id": 1,
            "SceneType": "Violence",
            "StartTime": "00:00:10",
            "EndTime": "00:00:16",
            "Blur": False,
        },
        {
            "Id": 2,
            "SceneType": "Nudity",
            "StartTime": "00:00:20",
            "EndTime": "00:00:21",
            "Blur": False,
        },
    ]


@pytest.mark.asyncio
async def test_indexed_title_load_reuses_persisted_index(tmp_path: Path, monkeypatch):
    export_dir = _write_sample_vidangel_export(tmp_path)
    await load_vidangel_raw_events_for_title("movie", "movie-1", export_dir=export_dir)

    monkeypatch.setattr(
        "leapfrog.vidangel_export._build_raw_event_index_sync",
        lambda *_args: pytest.fail("the valid persisted index should be reused"),
    )
    vidangel_export._raw_event_index_cache.clear()

    events = await load_vidangel_raw_events_for_title("movie", "movie-1", export_dir=export_dir)

    assert len(events) == 4


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
