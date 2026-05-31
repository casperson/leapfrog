"""Unit tests for skip-file conversion helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leapfrog.skip_file_converter import (
    convert_manifest_to_sidecars,
    build_sidecar_payload_from_import,
    parse_skp_text,
    parse_timestamp_html,
    parse_timestamp_text,
    sidecar_to_skp_text,
)


def test_parse_skp_text_extracts_segments_and_ignores_metadata():
    raw = """0:00:12.75
searchlight passes before O

0:48:38 --> 0:48:56
sex 2 (dream)

{"local":0.041666666666666664,"faselhdwatch":0}

data:image/jpeg;base64,AAAA
"""

    segments = parse_skp_text(raw)

    assert len(segments) == 2
    assert segments[0].start_time == pytest.approx(12.75)
    assert segments[0].end_time == pytest.approx(12.75)
    assert segments[0].text_excerpt == "searchlight passes before O"
    assert segments[1].start_time == pytest.approx((48 * 60) + 38)
    assert segments[1].end_time == pytest.approx((48 * 60) + 56)
    assert segments[1].text_excerpt == "sex 2 (dream)"


def test_parse_timestamp_text_extracts_range_blocks():
    raw = """0:10:00 --> 0:10:12
first scene

0:11:00 --> 0:11:05
second scene
"""

    segments = parse_timestamp_text(raw)

    assert len(segments) == 2
    assert segments[0].text_excerpt == "first scene"
    assert segments[1].text_excerpt == "second scene"


def test_parse_timestamp_html_extracts_visible_timestamp_blocks():
    raw = """
    <html>
      <body>
        <div><p>0:12:34 --&gt; 0:12:45</p><p>bathroom fight</p></div>
        <div><p>0:14:01</p><p>jump scare</p></div>
      </body>
    </html>
    """

    segments = parse_timestamp_html(raw)

    assert len(segments) == 2
    assert segments[0].start_time == pytest.approx((12 * 60) + 34)
    assert segments[0].end_time == pytest.approx((12 * 60) + 45)
    assert segments[1].start_time == pytest.approx((14 * 60) + 1)
    assert segments[1].end_time == pytest.approx((14 * 60) + 1)


def test_build_sidecar_payload_from_import_uses_leapfrog_sidecar_shape():
    payload = build_sidecar_payload_from_import(
        media_id="fight-club",
        title="Fight Club",
        segments=parse_timestamp_text("0:48:38 --> 0:48:56\nsex 2 (dream)\n"),
        category="sex_nudity_immodesty",
        source="skp_import",
    )

    assert payload["format"] == "leapfrog.segment.sidecar/v1"
    assert payload["media_id"] == "fight-club"
    assert payload["segments"][0]["category"] == "sex_nudity_immodesty"
    assert payload["segments"][0]["source"] == "skp_import"
    assert payload["segments"][0]["text_excerpt"] == "sex 2 (dream)"


def test_sidecar_to_skp_text_emits_time_blocks_and_alignment_json():
    payload = {
        "format": "leapfrog.segment.sidecar/v1",
        "media_id": "fight-club",
        "title": "Fight Club",
        "segments": [
            {
                "media_id": "fight-club",
                "start_time": 12.75,
                "end_time": 12.75,
                "category": "sex_nudity_immodesty",
                "source": "imported",
                "confidence": None,
                "labels": "",
                "text_excerpt": "searchlight passes before O",
            },
            {
                "media_id": "fight-club",
                "start_time": 2918.0,
                "end_time": 2936.0,
                "category": "sex_nudity_immodesty",
                "source": "imported",
                "confidence": None,
                "labels": "",
                "text_excerpt": "sex 2 (dream)",
            },
        ],
    }

    raw = sidecar_to_skp_text(payload, append_alignment=json.dumps({"local": 0, "faselhdwatch": 0}))

    assert "0:00:12.75\nsearchlight passes before O" in raw
    assert "0:48:38 --> 0:48:56\nsex 2 (dream)" in raw
    assert '{"local": 0, "faselhdwatch": 0}' in raw


@pytest.mark.asyncio
async def test_convert_manifest_to_sidecars_builds_outputs(tmp_path: Path):
    skp_path = tmp_path / "fight_club.skp"
    skp_path.write_text("0:48:38 --> 0:48:56\nsex 2 (dream)\n", encoding="utf-8")
    output_path = tmp_path / "Fight_Club.leapfrog.json"
    manifest_path = tmp_path / "manifest.csv"
    manifest_path.write_text(
        "\n".join(
            [
                "Title,Input Type,Input,Output,Media Id,Category,Source",
                f"Fight Club,skp,{skp_path},{output_path},fight-club,sex_nudity_immodesty,skp_import",
            ]
        ),
        encoding="utf-8",
    )

    results = await convert_manifest_to_sidecars(str(manifest_path))

    assert len(results) == 1
    assert results[0].status == "ok"
    assert output_path.exists()


@pytest.mark.asyncio
async def test_convert_manifest_to_sidecars_reports_missing_required_columns(tmp_path: Path):
    manifest_path = tmp_path / "manifest.csv"
    manifest_path.write_text(
        "\n".join(
            [
                "Title,Input Type,Input,Output",
                "Fight Club,skp,,",
            ]
        ),
        encoding="utf-8",
    )

    results = await convert_manifest_to_sidecars(str(manifest_path))

    assert len(results) == 1
    assert results[0].status == "error"
    assert "Input Type, Input, and Output" in results[0].detail
