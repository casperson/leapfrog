"""Unit tests for dynamic VidAngel-backed category metadata."""

from __future__ import annotations

import json

import pytest

from leapfrog.preferences import get_category_metadata


@pytest.mark.asyncio
async def test_get_category_metadata_includes_vidangel_labels(monkeypatch, tmp_path):
    export_dir = tmp_path / "vidangel_exports"
    export_dir.mkdir()
    (export_dir / "tag_definitions.json").write_text(
        json.dumps(
            {
                "definitions": [
                    {
                        "key": "fuck",
                        "display_title": "f-word",
                        "mapped_category": "profanity",
                        "path_titles": ["Language", "Profanity", "f-word"],
                        "example_description": "f-word",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEAPFROG_VIDANGEL_EXPORT_DIR", str(export_dir))

    categories = await get_category_metadata()
    profanity = next(category for category in categories if category["key"] == "profanity")
    label_keys = {label["key"] for label in profanity["labels"]}

    assert "vidangel:fuck" in label_keys
