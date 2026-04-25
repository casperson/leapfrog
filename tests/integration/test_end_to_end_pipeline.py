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
from leapfrog.domain import MediaSegment, SampledFrame
from leapfrog.logger import clear_log_buffer, get_logger, setup_logging
from leapfrog.plex_client import ActiveSession
from leapfrog.preferences import get_effective_skip_segments, get_resolved_preferences_for_user
from leapfrog.scanner import scan_video
from leapfrog.segment_export import build_sidecar_payload, read_sidecar_file, write_sidecar_file


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


async def test_end_to_end_scan_export_and_adapter_resolution(tmp_path, http_client):
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
    nudity_detector.scan_frames = AsyncMock(
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
    sexual_content_detector = MagicMock()
    sexual_content_detector.category = "sexual_content"
    sexual_content_detector.scan_frames = AsyncMock(
        return_value=DetectorResult(
            category="sexual_content",
            source="semantic_clip",
            status="done",
            detail="Matched sexual-content prompts on shared frames.",
            segments=[
                MediaSegment(
                    media_id="guid-e2e",
                    title="Fixture Movie",
                    start_ms=3500,
                    end_ms=5500,
                    category="sexual_content",
                    source="semantic_clip",
                    confidence=0.84,
                    labels="heavy_making_out,intimate_touch",
                )
            ],
        )
    )
    violence_detector = MagicMock()
    violence_detector.category = "violence"
    violence_detector.scan_frames = AsyncMock(
        return_value=DetectorResult(
            category="violence",
            source="semantic_clip",
            status="done",
            detail="Matched violence prompts on shared frames.",
            segments=[
                MediaSegment(
                    media_id="guid-e2e",
                    title="Fixture Movie",
                    start_ms=6000,
                    end_ms=8500,
                    category="violence",
                    source="semantic_clip",
                    confidence=0.79,
                    labels="fight,weapon_threat",
                )
            ],
        )
    )
    drugs_detector = MagicMock()
    drugs_detector.category = "drugs"
    drugs_detector.scan_frames = AsyncMock(
        return_value=DetectorResult(
            category="drugs",
            source="semantic_clip",
            status="done",
            detail="Matched drug prompts on shared frames.",
            segments=[
                MediaSegment(
                    media_id="guid-e2e",
                    title="Fixture Movie",
                    start_ms=9000,
                    end_ms=11000,
                    category="drugs",
                    source="semantic_clip",
                    confidence=0.81,
                    labels="needle,drug_paraphernalia",
                )
            ],
        )
    )

    with patch("leapfrog.scanner.get_duration_ms", new=AsyncMock(return_value=10000)), patch(
        "leapfrog.scanner.sample_video_frames",
        new=AsyncMock(return_value=[SampledFrame(offset_ms=0, jpeg_bytes=b"jpeg")]),
    ), patch(
        "leapfrog.scanner.ensure_semantic_model_async",
        new=AsyncMock(),
    ), patch("leapfrog.scanner.NudityDetector", return_value=nudity_detector), patch(
        "leapfrog.scanner.SexualContentDetector",
        return_value=sexual_content_detector,
    ), patch(
        "leapfrog.scanner.ViolenceDetector",
        return_value=violence_detector,
    ), patch(
        "leapfrog.scanner.DrugsDetector",
        return_value=drugs_detector,
    ):
        await scan_video("guid-e2e", _config())

    segments = await db.get_segments_for_guid("guid-e2e")
    assert {segment["category"] for segment in segments} == {
        "nudity",
        "sexual_content",
        "profanity",
        "violence",
        "drugs",
    }
    assert any(segment["text_excerpt"] for segment in segments if segment["category"] == "profanity")
    assert any(segment["labels"] == "heavy_making_out,intimate_touch" for segment in segments if segment["category"] == "sexual_content")
    assert any(segment["labels"] == "fight,weapon_threat" for segment in segments if segment["category"] == "violence")

    status_resp = await http_client.get(
        "/api/titles/guid-e2e/scan-status",
        params={"user": "alice"},
    )
    assert status_resp.status_code == 200
    status_payload = status_resp.json()
    assert status_payload["analysis_state"] == "scanned_flagged"
    assert status_payload["segment_counts_by_category"] == {
        "nudity": 1,
        "sexual_content": 1,
        "profanity": 1,
        "violence": 1,
        "drugs": 1,
    }

    await db.set_user_preference("alice", "nudity", enabled=False, threshold=0.5)
    await db.set_user_preference("alice", "sexual_content", enabled=False, threshold=0.5)
    await db.set_user_preference("alice", "profanity", enabled=True, threshold=0.5)
    await db.set_user_preference("alice", "violence", enabled=True, threshold=0.5)
    await db.set_user_preference("alice", "drugs", enabled=False, threshold=0.5)

    preferences = await get_resolved_preferences_for_user(
        "alice",
        threshold_defaults={
            "nudity": 0.4,
            "sexual_content": 0.5,
            "profanity": 0.5,
            "violence": 0.5,
            "drugs": 0.5,
        },
    )
    effective_segments = get_effective_skip_segments(segments, preferences)
    assert [segment["category"] for segment in effective_segments] == ["profanity", "violence"]

    detail_resp = await http_client.get(
        "/api/titles/guid-e2e/scan-details",
        params={"user": "alice"},
    )
    assert detail_resp.status_code == 200
    detail_payload = detail_resp.json()
    assert detail_payload["effective_segment_count"] == 2
    assert {row["category"] for row in detail_payload["scan_statuses"]} == {
        "nudity",
        "sexual_content",
        "profanity",
        "violence",
        "drugs",
    }
    assert {row["stage_key"] for row in detail_payload["stage_statuses"]} >= {
        "prepare",
        "nudity",
        "sexual_content",
        "profanity",
        "violence",
        "drugs",
        "finalize",
    }

    segment_resp = await http_client.get(
        "/api/titles/guid-e2e/segments",
        params={"user": "alice"},
    )
    assert segment_resp.status_code == 200
    segment_payload = segment_resp.json()["segments"]
    would_skip = {
        segment["category"]: segment["would_skip"]
        for segment in segment_payload
    }
    assert would_skip == {
        "nudity": False,
        "sexual_content": False,
        "profanity": True,
        "violence": True,
        "drugs": False,
    }

    sidecar_payload = await build_sidecar_payload("guid-e2e", user_id="alice")
    sidecar_path = await write_sidecar_file(media_path.with_suffix(".leapfrog.json"), sidecar_payload)
    restored_sidecar = await read_sidecar_file(sidecar_path)
    assert restored_sidecar["format"] == "leapfrog.segment.sidecar/v1"
    assert [segment["category"] for segment in restored_sidecar["segments"]] == [
        "profanity",
        "violence",
    ]

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
    assert [segment["category"] for segment in plex_context.effective_segments] == ["profanity", "violence"]
    assert [segment["category"] for segment in emby_context.effective_segments] == ["profanity", "violence"]
    assert [segment["category"] for segment in jellyfin_context.effective_segments] == ["profanity", "violence"]


async def test_end_to_end_queue_title_detail_and_log_routes(http_client):
    clear_log_buffer()
    setup_logging("INFO", buffer_capacity=50)
    app_logger = get_logger("leapfrog.tests.e2e")

    for guid in ("queue-a", "queue-b", "queue-c"):
        await db.upsert_scan_job(
            plex_guid=guid,
            title=f"Title {guid[-1].upper()}",
            file_path=f"/tmp/{guid}.mkv",
            rating_key=guid,
            library_id="lib-queue",
            library_title="Queue Fixtures",
        )

    await db.queue_scan_job("queue-a")
    await db.queue_scan_job("queue-b")
    await db.queue_scan_job("queue-c")
    await db.upsert_media_scan_status(
        "queue-c",
        "profanity",
        "done",
        source="subtitles",
        detail="Matched 1 subtitle profanity segment.",
        segment_count=1,
        progress=1.0,
    )
    await db.upsert_media_scan_status(
        "queue-c",
        "nudity",
        "pending",
        source="nudenet",
        detail="Waiting for detector.",
        segment_count=0,
        progress=0.0,
    )
    await db.upsert_media_scan_stage_status(
        "queue-c",
        "prepare",
        "done",
        source="scanner",
        detail="Prepared local scan context.",
        progress=1.0,
    )
    await db.upsert_media_scan_stage_status(
        "queue-c",
        "profanity",
        "done",
        category="profanity",
        source="subtitles",
        detail="Matched 1 profanity root.",
        progress=1.0,
    )
    await db.upsert_media_scan_stage_status(
        "queue-c",
        "nudity",
        "pending",
        category="nudity",
        source="nudenet",
        detail="Waiting for detector.",
        progress=0.0,
    )
    app_logger.info("queue api flow ready")

    queue_resp = await http_client.get("/api/scan/queue")
    assert queue_resp.status_code == 200
    assert [job["plex_guid"] for job in queue_resp.json()["jobs"]] == [
        "queue-a",
        "queue-b",
        "queue-c",
    ]

    move_resp = await http_client.post("/api/scan/queue/queue-c/move-top")
    assert move_resp.status_code == 200
    assert [job["plex_guid"] for job in move_resp.json()["jobs"]] == [
        "queue-c",
        "queue-a",
        "queue-b",
    ]

    cancel_resp = await http_client.post(
        "/api/scan/queue/cancel-selected",
        json={"guids": ["queue-a", "queue-b"]},
    )
    assert cancel_resp.status_code == 200
    assert [job["plex_guid"] for job in cancel_resp.json()["jobs"]] == ["queue-c"]

    detail_resp = await http_client.get("/api/titles/queue-c/scan-details")
    assert detail_resp.status_code == 200
    detail_payload = detail_resp.json()
    assert detail_payload["queue"]["state"] == "queued"
    assert detail_payload["analysis_state"] == "partially_scanned"
    assert detail_payload["segment_counts_by_category"]["profanity"] == 1
    assert [row["stage_key"] for row in detail_payload["stage_statuses"]] == [
        "prepare",
        "nudity",
        "profanity",
    ]

    history_resp = await http_client.get(
        "/api/logs/history",
        params={"source": "leapfrog.tests.e2e", "limit": 10},
    )
    assert history_resp.status_code == 200
    assert history_resp.json()["entries"][-1]["message"] == "queue api flow ready"

    async with http_client.stream(
        "GET",
        "/api/logs/stream",
        params={"source": "leapfrog.tests.e2e", "follow": "false", "replay": 10},
    ) as resp:
        body = await resp.aread()
    assert resp.status_code == 200
    assert '"message": "queue api flow ready"' in body.decode("utf-8")
