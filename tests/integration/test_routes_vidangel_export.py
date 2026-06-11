from __future__ import annotations

import json
from pathlib import Path

import pytest

import leapfrog.vidangel_export as vidangel_export


pytestmark = pytest.mark.usefixtures("setup_db")


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
                        "event_count": 2,
                        "category_count": 1,
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
                "movie-1,movie,Sample Movie,sample-movie,vidangel,,,1,language/profanity/shit,Language > Profanity > s-word,s-word,s-word,audio,4000,4000,language_profanity,shit",
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


async def test_vidangel_export_catalog_and_filter_routes(http_client, tmp_path, monkeypatch):
    export_dir = _write_sample_vidangel_export(tmp_path)
    monkeypatch.setattr(vidangel_export, "get_vidangel_export_dir", lambda: export_dir)

    catalog_resp = await http_client.get("/api/vidangel/export/catalog")
    assert catalog_resp.status_code == 200
    catalog = catalog_resp.json()
    assert catalog["available"] is True
    assert catalog["title_count"] == 1

    filters_resp = await http_client.get("/api/vidangel/export/titles/movie-1/filters")
    assert filters_resp.status_code == 200
    filters = filters_resp.json()
    assert filters["title"]["title"] == "Sample Movie"
    assert filters["leaf_count"] == 2
    assert filters["categories"][0]["label"] == "Profanity"


async def test_vidangel_export_skp_route_returns_attachment(http_client, tmp_path, monkeypatch):
    export_dir = _write_sample_vidangel_export(tmp_path)
    monkeypatch.setattr(vidangel_export, "get_vidangel_export_dir", lambda: export_dir)

    response = await http_client.post(
        "/api/vidangel/export/titles/movie-1/skp",
        json={"selected_leaf_keys": ["fuck"]},
    )

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "0:00:01" in response.text
    assert "s-word" not in response.text


async def test_vidangel_export_skp_route_reports_missing_export_data(http_client, tmp_path, monkeypatch):
    empty_export_dir = tmp_path / "empty"
    empty_export_dir.mkdir()
    monkeypatch.setattr(vidangel_export, "get_vidangel_export_dir", lambda: empty_export_dir)

    response = await http_client.post(
        "/api/vidangel/export/titles/movie-1/skp",
        json={"selected_leaf_keys": ["fuck"]},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "VidAngel title not found"


async def test_vidangel_export_filter_route_reports_missing_raw_events(http_client, tmp_path, monkeypatch):
    export_dir = _write_sample_vidangel_export_without_raw_events(tmp_path)
    monkeypatch.setattr(vidangel_export, "get_vidangel_export_dir", lambda: export_dir)

    response = await http_client.get("/api/vidangel/export/titles/movie-1/filters")

    assert response.status_code == 503
    assert response.json()["detail"] == "VidAngel raw filter events export is missing."


async def test_vidangel_export_skp_route_reports_missing_raw_events(http_client, tmp_path, monkeypatch):
    export_dir = _write_sample_vidangel_export_without_raw_events(tmp_path)
    monkeypatch.setattr(vidangel_export, "get_vidangel_export_dir", lambda: export_dir)

    response = await http_client.post(
        "/api/vidangel/export/titles/movie-1/skp",
        json={"selected_leaf_keys": ["fuck"]},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "VidAngel raw filter events export is missing."
