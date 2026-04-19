"""Integration tests for segment-related API routes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from leapfrog import database as db
from tests.conftest import make_mock_plex_client


pytestmark = pytest.mark.usefixtures("setup_db")


# ── GET /api/titles/{plex_guid}/segments ──────────────────────────────────────

async def test_get_segments_for_title_returns_empty(http_client):
    resp = await http_client.get("/api/titles/unknown-guid/segments")
    assert resp.status_code == 200
    assert resp.json()["segments"] == []


async def test_get_segments_for_title_returns_rows(http_client):
    await db.insert_segment("guid-seg", "Movie", start_ms=1000, end_ms=5000, confidence=0.9, labels="NUDITY")
    resp = await http_client.get("/api/titles/guid-seg/segments")
    assert resp.status_code == 200
    segs = resp.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["start_ms"] == 1000
    assert segs[0]["end_ms"] == 5000


async def test_get_segments_for_title_returns_extended_fields(http_client):
    await db.insert_segment(
        "guid-seg-extended",
        "Movie",
        start_ms=2000,
        end_ms=6000,
        category="profanity",
        source="subtitles",
        confidence=0.95,
        text_excerpt="a profanity example",
    )
    resp = await http_client.get("/api/titles/guid-seg-extended/segments")
    segment = resp.json()["segments"][0]
    assert segment["category"] == "profanity"
    assert segment["source"] == "subtitles"
    assert segment["text_excerpt"] == "a profanity example"


async def test_get_segments_for_title_supports_category_filter(http_client):
    await db.insert_segment(
        "guid-seg-filter",
        "Movie",
        start_ms=2000,
        end_ms=6000,
        category="profanity",
        source="subtitles",
        confidence=0.95,
        text_excerpt="a profanity example",
    )
    await db.insert_segment(
        "guid-seg-filter",
        "Movie",
        start_ms=8000,
        end_ms=12000,
        category="violence",
        source="semantic_clip",
        confidence=0.8,
        labels="fight,weapon",
    )

    resp = await http_client.get(
        "/api/titles/guid-seg-filter/segments",
        params=[("category", "violence")],
    )

    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert len(segments) == 1
    assert segments[0]["category"] == "violence"


async def test_get_segments_for_title_preserves_semantic_labels_outside_nudenet_filter(http_client):
    await db.set_setting("scan_labels", '["FEMALE_BREAST_EXPOSED"]')
    await db.insert_segment(
        "guid-seg-semantic-labels",
        "Movie",
        start_ms=8000,
        end_ms=12000,
        category="violence",
        source="semantic_clip",
        confidence=0.8,
        labels="fight,weapon",
    )

    resp = await http_client.get("/api/titles/guid-seg-semantic-labels/segments")

    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert len(segments) == 1
    assert segments[0]["labels"] == "fight,weapon"


async def test_get_segments_for_title_can_report_whether_user_would_skip(http_client):
    await db.insert_segment(
        "guid-would-skip",
        "Movie",
        start_ms=2000,
        end_ms=6000,
        category="profanity",
        source="subtitles",
        confidence=0.95,
    )
    await db.set_user_preference("alice", "nudity", enabled=True, threshold=0.5)
    await db.set_user_preference("alice", "profanity", enabled=False, threshold=0.5)
    resp = await http_client.get("/api/titles/guid-would-skip/segments?user=alice")
    assert resp.status_code == 200
    segment = resp.json()["segments"][0]
    assert segment["would_skip"] is False


async def test_get_segments_for_title_has_thumbnail_url_when_path_set(http_client):
    seg_id = await db.insert_segment(
        "guid-thumb", "T", start_ms=0, end_ms=1000, thumbnail_path="/some/path.jpg"
    )
    resp = await http_client.get("/api/titles/guid-thumb/segments")
    segs = resp.json()["segments"]
    assert segs[0]["has_thumbnail"] is True
    assert f"/api/thumbnails/{seg_id}" in segs[0]["thumbnail_url"]


async def test_get_export_for_title_returns_canonical_payload(http_client):
    await db.upsert_scan_job(
        plex_guid="guid-export-route",
        title="Route Export Movie",
        file_path="/route-export.mkv",
        rating_key="200",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-export-route",
        "Route Export Movie",
        start_ms=1000,
        end_ms=5000,
        category="profanity",
        source="subtitles",
        confidence=0.9,
        text_excerpt="route snippet",
    )

    resp = await http_client.get("/api/titles/guid-export-route/export")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["format"] == "leapfrog.segment.export/v1"
    assert payload["media_id"] == "guid-export-route"
    assert payload["segments"][0]["category"] == "profanity"
    assert payload["segments"][0]["text_excerpt"] == "route snippet"


async def test_get_export_for_title_supports_user_and_adapter_filters(http_client):
    await db.upsert_scan_job(
        plex_guid="guid-adapter-route",
        title="Adapter Route Movie",
        file_path="/adapter-route.mkv",
        rating_key="201",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-adapter-route",
        "Adapter Route Movie",
        start_ms=1000,
        end_ms=5000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )
    await db.insert_segment(
        "guid-adapter-route",
        "Adapter Route Movie",
        start_ms=6000,
        end_ms=9000,
        category="profanity",
        source="subtitles",
        confidence=0.95,
    )
    await db.set_user_preference("alice", "nudity", enabled=False, threshold=0.5)
    await db.set_user_preference("alice", "profanity", enabled=True, threshold=0.5)

    resp = await http_client.get(
        "/api/titles/guid-adapter-route/export",
        params=[("user", "alice"), ("adapter", "sidecar"), ("category", "profanity")],
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["format"] == "leapfrog.segment.sidecar/v1"
    assert len(payload["segments"]) == 1
    assert payload["segments"][0]["category"] == "profanity"


async def test_get_scan_status_for_title_returns_status_and_counts(http_client):
    await db.upsert_scan_job(
        plex_guid="guid-status-route",
        title="Status Route Movie",
        file_path="/status-route.mkv",
        rating_key="202",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-status-route",
        "Status Route Movie",
        start_ms=1000,
        end_ms=5000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )
    await db.upsert_media_scan_status(
        "guid-status-route",
        "nudity",
        "done",
        source="nudenet",
        detail="1 segment",
    )

    resp = await http_client.get("/api/titles/guid-status-route/scan-status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["media_id"] == "guid-status-route"
    assert payload["segment_counts_by_category"]["nudity"] == 1
    assert any(row["category"] == "nudity" for row in payload["scan_statuses"])
    assert payload["last_scan_time"] is None
    assert payload["export_available"] is True


async def test_get_scan_status_for_title_can_report_preference_resolution(http_client):
    await db.upsert_scan_job(
        plex_guid="guid-status-pref",
        title="Status Pref Movie",
        file_path="/status-pref.mkv",
        rating_key="203",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-status-pref",
        "Status Pref Movie",
        start_ms=1000,
        end_ms=5000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )
    await db.insert_segment(
        "guid-status-pref",
        "Status Pref Movie",
        start_ms=6000,
        end_ms=9000,
        category="profanity",
        source="subtitles",
        confidence=0.9,
    )
    await db.set_user_preference("alice", "nudity", enabled=False, threshold=0.5)
    await db.set_user_preference("alice", "profanity", enabled=True, threshold=0.5)

    resp = await http_client.get("/api/titles/guid-status-pref/scan-status?user=alice")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["preference_resolution_success"] is True
    assert payload["effective_segment_count"] == 1


async def test_scan_status_includes_partially_scanned_titles_with_zero_segments(http_client):
    await db.upsert_scan_job(
        plex_guid="guid-partial-zero",
        title="Partial Zero Movie",
        file_path="/partial-zero.mkv",
        rating_key="204",
        library_id="lib1",
        library_title="Movies",
    )
    await db.upsert_media_scan_status(
        "guid-partial-zero",
        "nudity",
        "done",
        source="nudenet",
        detail="No segments",
        segment_count=0,
        progress=1.0,
    )
    await db.upsert_media_scan_status(
        "guid-partial-zero",
        "profanity",
        "running",
        source="subtitles",
        detail="Detector running.",
        segment_count=0,
        progress=0.4,
    )

    resp = await http_client.get("/api/titles/guid-partial-zero/scan-status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["analysis_state"] == "partially_scanned"
    assert payload["segment_counts_by_category"]["nudity"] == 0


async def test_scan_details_returns_stage_statuses_and_queue_state(http_client):
    await db.upsert_scan_job(
        plex_guid="guid-scan-detail",
        title="Scan Detail Movie",
        file_path="/scan-detail.mkv",
        rating_key="205",
        library_id="lib1",
        library_title="Movies",
    )
    await db.queue_scan_job("guid-scan-detail")
    await db.upsert_media_scan_stage_status(
        "guid-scan-detail",
        "prepare",
        "done",
        source="scanner",
        detail="Prepared",
        progress=1.0,
    )

    resp = await http_client.get("/api/titles/guid-scan-detail/scan-details")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["queue"]["state"] == "queued"
    assert payload["stage_statuses"][0]["stage_key"] == "prepare"


async def test_scan_timeline_returns_stage_rows(http_client):
    await db.upsert_scan_job(
        plex_guid="guid-scan-timeline",
        title="Scan Timeline Movie",
        file_path="/scan-timeline.mkv",
        rating_key="206",
        library_id="lib1",
        library_title="Movies",
    )
    await db.upsert_media_scan_stage_status(
        "guid-scan-timeline",
        "prepare",
        "done",
        source="scanner",
        detail="Prepared",
        progress=1.0,
    )

    resp = await http_client.get("/api/titles/guid-scan-timeline/scan-timeline")

    assert resp.status_code == 200
    assert resp.json()["timeline"][0]["stage_key"] == "prepare"


# ── DELETE /api/segments/{segment_id} ─────────────────────────────────────────

async def test_delete_segment_returns_ok(http_client):
    seg_id = await db.insert_segment("guid-del", "M", start_ms=0, end_ms=1000)
    with patch("leapfrog.web.routes.segments._refresh_leapfrog_summary_for_guid"):
        resp = await http_client.delete(f"/api/segments/{seg_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_delete_segment_returns_404_for_missing(http_client):
    with patch("leapfrog.web.routes.segments._refresh_leapfrog_summary_for_guid"):
        resp = await http_client.delete("/api/segments/99999")
    assert resp.status_code == 404


async def test_delete_segment_actually_removes_row(http_client):
    seg_id = await db.insert_segment("guid-del2", "M", start_ms=0, end_ms=1000)
    with patch("leapfrog.web.routes.segments._refresh_leapfrog_summary_for_guid"):
        await http_client.delete(f"/api/segments/{seg_id}")
    assert await db.get_segment_by_id(seg_id) is None


async def test_delete_segment_removes_thumbnail_file(http_client, tmp_path):
    thumbnail_path = tmp_path / "segment-thumb.jpg"
    thumbnail_path.write_bytes(b"thumb")
    seg_id = await db.insert_segment(
        "guid-del-thumb",
        "M",
        start_ms=0,
        end_ms=1000,
        thumbnail_path=str(thumbnail_path),
    )
    with patch("leapfrog.web.routes.segments._refresh_leapfrog_summary_for_guid"):
        resp = await http_client.delete(f"/api/segments/{seg_id}")
    assert resp.status_code == 200
    assert thumbnail_path.exists() is False


# ── DELETE /api/titles/{plex_guid}/segments ───────────────────────────────────

async def test_delete_all_segments_for_title(http_client):
    await db.insert_segment("guid-bulk", "M", start_ms=0, end_ms=1000)
    await db.insert_segment("guid-bulk", "M", start_ms=2000, end_ms=3000)
    with patch("leapfrog.web.routes.segments._refresh_leapfrog_summary_for_guid"):
        resp = await http_client.delete("/api/titles/guid-bulk/segments")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert await db.get_segments_for_guid("guid-bulk") == []


async def test_delete_all_segments_for_title_removes_thumbnail_files(http_client, tmp_path):
    first_thumb = tmp_path / "bulk-thumb-1.jpg"
    second_thumb = tmp_path / "bulk-thumb-2.jpg"
    first_thumb.write_bytes(b"thumb-1")
    second_thumb.write_bytes(b"thumb-2")
    await db.insert_segment(
        "guid-bulk-thumb",
        "M",
        start_ms=0,
        end_ms=1000,
        thumbnail_path=str(first_thumb),
    )
    await db.insert_segment(
        "guid-bulk-thumb",
        "M",
        start_ms=2000,
        end_ms=3000,
        thumbnail_path=str(second_thumb),
    )
    with patch("leapfrog.web.routes.segments._refresh_leapfrog_summary_for_guid"):
        resp = await http_client.delete("/api/titles/guid-bulk-thumb/segments")
    assert resp.status_code == 200
    assert first_thumb.exists() is False
    assert second_thumb.exists() is False


async def test_delete_all_segments_returns_zero_when_none(http_client):
    with patch("leapfrog.web.routes.segments._refresh_leapfrog_summary_for_guid"):
        resp = await http_client.delete("/api/titles/nonexistent/segments")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0


# ── GET /api/segments ─────────────────────────────────────────────────────────

async def test_get_all_segments_empty(http_client):
    resp = await http_client.get("/api/segments")
    assert resp.status_code == 200
    assert resp.json()["segments"] == []


async def test_get_all_segments_pagination(http_client):
    for i in range(5):
        await db.insert_segment(f"guid-page-{i}", "T", start_ms=i * 1000, end_ms=i * 1000 + 500)
    resp = await http_client.get("/api/segments?limit=3&offset=0")
    assert resp.status_code == 200
    assert len(resp.json()["segments"]) == 3


# ── GET /api/libraries ─────────────────────────────────────────────────────────

async def test_get_libraries_returns_error_when_plex_not_configured(http_client):
    with patch("leapfrog.web.routes.segments.plex_mod.get_client", side_effect=RuntimeError("not configured")):
        resp = await http_client.get("/api/libraries")
    assert resp.status_code == 200
    data = resp.json()
    assert data["libraries"] == []
    assert "error" in data


async def test_get_libraries_returns_sections(http_client):
    from leapfrog.plex_client import LibrarySection
    mock_client = make_mock_plex_client(
        library_sections=[LibrarySection("1", "Movies", "movie"), LibrarySection("2", "Shows", "show")]
    )
    with patch("leapfrog.web.routes.segments.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/libraries")
    assert resp.status_code == 200
    libs = resp.json()["libraries"]
    assert len(libs) == 2
    assert libs[0]["id"] == "1"


# ── GET /api/libraries/{library_id}/titles ────────────────────────────────────

async def test_get_titles_in_library_returns_empty_for_no_jobs(http_client):
    with patch("leapfrog.web.routes.segments.plex_mod.get_client", side_effect=RuntimeError):
        resp = await http_client.get("/api/libraries/lib1/titles")
    assert resp.status_code == 200
    assert resp.json()["titles"] == []


async def test_get_titles_in_library_returns_jobs(http_client):
    await db.upsert_scan_job(
        plex_guid="title-guid",
        title="Test Movie",
        file_path="/test.mkv",
        rating_key="1",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment("title-guid", "Test Movie", start_ms=0, end_ms=1000)

    mock_client = make_mock_plex_client()
    with patch("leapfrog.web.routes.segments.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/libraries/lib1/titles")

    assert resp.status_code == 200
    titles = resp.json()["titles"]
    assert len(titles) == 1
    assert titles[0]["plex_guid"] == "title-guid"
    assert titles[0]["segment_count"] == 1


# ── POST /api/libraries/{library_id}/sync ─────────────────────────────────────

async def test_sync_library_returns_error_when_plex_not_configured(http_client):
    with patch("leapfrog.web.routes.segments.plex_mod.get_client", side_effect=RuntimeError("not set")):
        resp = await http_client.post("/api/libraries/lib1/sync")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


async def test_sync_library_adds_new_titles(http_client):
    from leapfrog.plex_client import MediaItem
    items = [
        MediaItem(
            rating_key="100",
            plex_guid="sync-g1",
            title="New Movie",
            year=2024,
            thumb="/t",
            file_path="/new.mkv",
            library_id="lib2",
            library_title="Movies",
            media_type="movie",
            content_rating="R",
        )
    ]
    mock_client = make_mock_plex_client(library_items=items)
    with patch("leapfrog.web.routes.segments.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/libraries/lib2/sync")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["new"] == 1
