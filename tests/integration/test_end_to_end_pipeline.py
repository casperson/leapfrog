"""End-to-end tests for scan, storage, export, and adapter resolution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leapfrog import database as db
from leapfrog.adapters.emby_runtime import EmbyPlaybackReference, resolve_emby_playback_context
from leapfrog.adapters.jellyfin_runtime import JellyfinPlaybackReference, resolve_jellyfin_playback_context
from leapfrog.adapters.plex_runtime import resolve_plex_playback_context
from leapfrog.detectors.base import DetectorResult
from leapfrog.domain import MediaSegment
from leapfrog.plex_client import ActiveSession
from leapfrog.preferences import get_effective_skip_segments, get_resolved_preferences_for_user
from leapfrog.scanner import scan_video
from leapfrog.segment_export import build_sidecar_payload, read_sidecar_file, write_sidecar_file


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
        profanity_terms=["damn"],
        profanity_merge_gap_ms=1500,
        whisper_enabled=False,
        whisper_model="base",
        is_scan_window=lambda: True,
    )


def _plex_session(media_path: Path) -> ActiveSession:
    return ActiveSession(
        session_key="sess-e2e",
        user="alice",
        title="Fixture Movie",
        full_title="Fixture Movie",
        plex_guid="guid-e2e",
        rating_key="",
        media_type="movie",
        position_ms=1000,
        duration_ms=90000,
        client_identifier="client-1",
        client_title="Plex Web",
        is_controllable=True,
        file_path=str(media_path),
    )


async def test_end_to_end_scan_export_and_adapter_resolution(tmp_path):
    media_path = tmp_path / "fixture.mkv"
    media_path.write_bytes(b"fake")
    subtitle_fixture = Path(__file__).resolve().parent.parent / "fixtures" / "sample_profanity.srt"
    subtitle_path = media_path.with_suffix(".srt")
    subtitle_path.write_text(subtitle_fixture.read_text(encoding="utf-8"), encoding="utf-8")

    await db.upsert_scan_job(
        plex_guid="guid-e2e",
        title="Fixture Movie",
        file_path=str(media_path),
        rating_key="",
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
                    media_id="guid-e2e",
                    title="Fixture Movie",
                    start_ms=1000,
                    end_ms=3000,
                    category="nudity",
                    source="nudenet",
                    confidence=0.9,
                )
            ],
        )
    )

    with patch("leapfrog.scanner.NudityDetector", return_value=nudity_detector):
        await scan_video("guid-e2e", _config())

    segments = await db.get_segments_for_guid("guid-e2e")
    assert {segment["category"] for segment in segments} == {"nudity", "profanity"}
    assert any(segment["text_excerpt"] for segment in segments if segment["category"] == "profanity")

    await db.set_user_preference("alice", "nudity", enabled=False, threshold=0.5)
    await db.set_user_preference("alice", "profanity", enabled=True, threshold=0.5)

    preferences = await get_resolved_preferences_for_user(
        "alice",
        nudity_threshold=0.6,
        profanity_threshold=0.5,
    )
    effective_segments = get_effective_skip_segments(segments, preferences)
    assert len(effective_segments) == 1
    assert effective_segments[0]["category"] == "profanity"

    sidecar_payload = await build_sidecar_payload("guid-e2e", user_id="alice")
    sidecar_path = await write_sidecar_file(media_path.with_suffix(".leapfrog.json"), sidecar_payload)
    restored_sidecar = await read_sidecar_file(sidecar_path)
    assert restored_sidecar["format"] == "leapfrog.segment.sidecar/v1"
    assert len(restored_sidecar["segments"]) == 1
    assert restored_sidecar["segments"][0]["category"] == "profanity"

    plex_context = await resolve_plex_playback_context(_plex_session(media_path))
    emby_context = await resolve_emby_playback_context(
        EmbyPlaybackReference(
            user_name="alice",
            item_id="emby-e2e",
            title="Fixture Movie",
            file_path=str(media_path),
            canonical_media_id="guid-e2e",
        )
    )
    jellyfin_context = await resolve_jellyfin_playback_context(
        JellyfinPlaybackReference(
            user_name="alice",
            item_id="jellyfin-e2e",
            title="Fixture Movie",
            file_path=str(media_path),
            canonical_media_id="guid-e2e",
        )
    )

    assert plex_context.segment_source == "sidecar+db"
    assert emby_context.segment_source == "sidecar+db"
    assert jellyfin_context.segment_source == "sidecar+db"
    assert [segment["category"] for segment in plex_context.effective_segments] == ["profanity"]
    assert [segment["category"] for segment in emby_context.effective_segments] == ["profanity"]
    assert [segment["category"] for segment in jellyfin_context.effective_segments] == ["profanity"]
