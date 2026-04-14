"""Unit tests for detector orchestration in scanner.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leapfrog import database as db
from leapfrog.detectors.base import DetectorResult
from leapfrog.domain import MediaSegment
import leapfrog.scanner as scanner


pytestmark = pytest.mark.usefixtures("setup_db")


def _config():
    return SimpleNamespace(
        scan_ratings=[],
        confidence_threshold=0.6,
        default_profanity_threshold=0.5,
        scan_step_ms=5000,
        segment_gap_ms=12000,
        segment_min_hits=1,
        scan_labels=[],
        nudenet_model="320n",
        nudenet_model_path="",
        profanity_terms=["shit"],
        profanity_merge_gap_ms=1500,
        whisper_enabled=False,
        whisper_model="base",
        is_scan_window=lambda: True,
    )


async def test_scan_video_persists_detector_results(tmp_path):
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
    nudity_detector.scan = AsyncMock(
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
                )
            ],
        )
    )

    mock_client = MagicMock()
    mock_client.update_leapfrog_summary = AsyncMock(return_value=True)

    with patch("leapfrog.scanner.NudityDetector", return_value=nudity_detector), patch(
        "leapfrog.scanner.ProfanityDetector",
        return_value=profanity_detector,
    ), patch(
        "leapfrog.scanner.plex_mod.get_client",
        return_value=mock_client,
    ):
        await scanner.scan_video("guid-scan", _config())

    segments = await db.get_segments_for_guid("guid-scan")
    assert {segment["category"] for segment in segments} == {"nudity", "profanity"}

    statuses = await db.get_media_scan_statuses_for_media("guid-scan")
    assert {row["status"] for row in statuses} == {"done"}
    assert any(
        row["category"] == "profanity" and row["source"] == "subtitles"
        for row in statuses
    )
