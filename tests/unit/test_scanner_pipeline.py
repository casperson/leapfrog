"""Unit tests for detector orchestration in scanner.py."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leapfrog import database as db
from leapfrog.detectors.base import DetectorResult
from leapfrog.domain import MediaSegment, SampledFrame
import leapfrog.scanner as scanner


pytestmark = pytest.mark.usefixtures("setup_db")


def _config():
    return SimpleNamespace(
        scan_ratings=[],
        confidence_threshold=0.4,
        default_profanity_threshold=0.5,
        scan_step_ms=250,
        segment_gap_ms=12000,
        segment_min_hits=1,
        scan_labels=[],
        nudenet_model="640m",
        nudenet_model_path="",
        profanity_terms=["shit"],
        profanity_merge_gap_ms=1500,
        whisper_enabled=False,
        whisper_model="base",
        is_scan_window=lambda: True,
    )


async def test_scan_video_persists_detector_results(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="leapfrog.scanner")
    media_path = tmp_path / "movie.mkv"
    media_path.write_bytes(b"fake")
    await db.upsert_scan_job(
        plex_guid="guid-scan",
        title="Movie",
        file_path=str(media_path),
        rating_key="101",
        library_id="lib-1",
        library_title="Movies",
    )

    nudity_detector = MagicMock()
    nudity_detector.category = "nudity"
    nudity_detector.scan_frames = AsyncMock(
        return_value=DetectorResult(
            category="nudity",
            source="nudenet",
            status="done",
            segments=[
                MediaSegment(
                    media_id="guid-scan",
                    title="Movie",
                    start_ms=1000,
                    end_ms=5000,
                    category="nudity",
                    source="nudenet",
                    confidence=0.9,
                    labels="FEMALE_BREAST_EXPOSED",
                )
            ],
        )
    )
    profanity_detector = MagicMock()
    profanity_detector.category = "profanity"
    profanity_detector.scan = AsyncMock(
        return_value=DetectorResult(
            category="profanity",
            source="subtitles",
            status="done",
            segments=[
                MediaSegment(
                    media_id="guid-scan",
                    title="Movie",
                    start_ms=7000,
                    end_ms=9000,
                    category="profanity",
                    source="subtitles",
                    confidence=0.75,
                    text_excerpt="bad word",
                    labels="fuck",
                )
            ],
        )
    )
    sexual_detector = MagicMock()
    sexual_detector.scan_frames = AsyncMock(
        return_value=DetectorResult(
            category="sexual_content",
            source="semantic_clip",
            status="done",
            segments=[],
        )
    )
    violence_detector = MagicMock()
    violence_detector.scan_frames = AsyncMock(
        return_value=DetectorResult(
            category="violence",
            source="semantic_clip",
            status="done",
            segments=[],
        )
    )
    drugs_detector = MagicMock()
    drugs_detector.scan_frames = AsyncMock(
        return_value=DetectorResult(
            category="drugs",
            source="semantic_clip",
            status="done",
            segments=[],
        )
    )

    mock_client = MagicMock()
    mock_client.update_leapfrog_summary = AsyncMock(return_value=True)
    frames = [SampledFrame(offset_ms=0, jpeg_bytes=b"jpeg")]

    with patch("leapfrog.scanner.get_duration_ms", new=AsyncMock(return_value=10000)), patch(
        "leapfrog.scanner.sample_video_frames",
        new=AsyncMock(return_value=frames),
    ), patch(
        "leapfrog.scanner.ensure_semantic_model_async",
        new=AsyncMock(),
    ) as sample_frames, patch("leapfrog.scanner.NudityDetector", return_value=nudity_detector), patch(
        "leapfrog.scanner.SexualContentDetector",
        return_value=sexual_detector,
    ), patch(
        "leapfrog.scanner.ViolenceDetector",
        return_value=violence_detector,
    ), patch(
        "leapfrog.scanner.DrugsDetector",
        return_value=drugs_detector,
    ), patch(
        "leapfrog.scanner.ProfanityDetector",
        return_value=profanity_detector,
    ), patch(
        "leapfrog.scanner.get_client",
        return_value=mock_client,
    ):
        await scanner.scan_video("guid-scan", _config())

    segments = await db.get_segments_for_guid("guid-scan")
    assert {segment["category"] for segment in segments} == {"sex_nudity_immodesty", "language_profanity_captions"}

    statuses = await db.get_media_scan_statuses_for_media("guid-scan")
    status_by_category = {row["category"]: row for row in statuses}
    assert status_by_category["sex_nudity_immodesty"]["status"] == "done"
    assert status_by_category["language_profanity_captions"]["status"] == "done"
    assert any(
        row["category"] == "language_profanity_captions" and row["source"] == "subtitles"
        for row in statuses
    )
    stage_rows = await db.get_media_scan_stage_statuses_for_media("guid-scan")
    assert any(row["stage_key"] == "prepare" for row in stage_rows)
    sample_frames.assert_awaited_once()
    assert "Scan prepare complete: Movie" in caplog.text
    assert "Scan frame sampling complete: Movie (1 frame(s))" in caplog.text
    assert "Scan stage started: Movie — nudity (1/5)" in caplog.text
    assert "Scan stage complete: Movie — profanity source=subtitles segments=1" in caplog.text


async def test_scan_video_replaces_category_segments_and_deletes_stale_thumbnails(tmp_path):
    media_path = tmp_path / "movie-replace.mkv"
    media_path.write_bytes(b"fake")
    stale_thumbnail = tmp_path / "stale-violence.jpg"
    stale_thumbnail.write_bytes(b"old-thumb")
    await db.upsert_scan_job(
        plex_guid="guid-replace",
        title="Movie Replace",
        file_path=str(media_path),
        rating_key="102",
        library_id="lib-1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-replace",
        "Movie Replace",
        start_ms=4000,
        end_ms=8000,
        category="violence",
        source="semantic_clip",
        confidence=0.8,
        thumbnail_path=str(stale_thumbnail),
        labels="fight",
    )

    empty_image_result = DetectorResult(
        category="nudity",
        source="nudenet",
        status="done",
        segments=[],
    )
    nudity_detector = MagicMock()
    nudity_detector.category = "nudity"
    nudity_detector.scan_frames = AsyncMock(return_value=empty_image_result)
    sexual_detector = MagicMock()
    sexual_detector.scan_frames = AsyncMock(
        return_value=DetectorResult(
            category="sexual_content",
            source="semantic_clip",
            status="done",
            segments=[],
        )
    )
    violence_detector = MagicMock()
    violence_detector.scan_frames = AsyncMock(
        return_value=DetectorResult(
            category="violence",
            source="semantic_clip",
            status="done",
            segments=[],
        )
    )
    drugs_detector = MagicMock()
    drugs_detector.scan_frames = AsyncMock(
        return_value=DetectorResult(
            category="drugs",
            source="semantic_clip",
            status="done",
            segments=[],
        )
    )
    profanity_detector = MagicMock()
    profanity_detector.category = "profanity"
    profanity_detector.scan = AsyncMock(
        return_value=DetectorResult(
            category="profanity",
            source="subtitles",
            status="done",
            segments=[],
        )
    )

    mock_client = MagicMock()
    mock_client.update_leapfrog_summary = AsyncMock(return_value=True)

    with patch("leapfrog.scanner.get_duration_ms", new=AsyncMock(return_value=10000)), patch(
        "leapfrog.scanner.sample_video_frames",
        new=AsyncMock(return_value=[SampledFrame(offset_ms=0, jpeg_bytes=b"jpeg")]),
    ), patch(
        "leapfrog.scanner.ensure_semantic_model_async",
        new=AsyncMock(),
    ), patch("leapfrog.scanner.NudityDetector", return_value=nudity_detector), patch(
        "leapfrog.scanner.SexualContentDetector",
        return_value=sexual_detector,
    ), patch(
        "leapfrog.scanner.ViolenceDetector",
        return_value=violence_detector,
    ), patch(
        "leapfrog.scanner.DrugsDetector",
        return_value=drugs_detector,
    ), patch(
        "leapfrog.scanner.ProfanityDetector",
        return_value=profanity_detector,
    ), patch(
        "leapfrog.scanner.get_client",
        return_value=mock_client,
    ):
        await scanner.scan_video("guid-replace", _config())

    assert stale_thumbnail.exists() is False
    segments = await db.get_segments_for_guid("guid-replace")
    assert [segment["category"] for segment in segments] == []
