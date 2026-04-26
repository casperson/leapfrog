"""Unit tests for canonical segment export helpers and adapters."""

from __future__ import annotations

import pytest

from leapfrog import database as db
from leapfrog.adapters import get_segment_adapter
from leapfrog.segment_export import (
    build_canonical_media_export,
    build_sidecar_payload,
    read_sidecar_file,
    write_sidecar_file,
)


pytestmark = pytest.mark.usefixtures("setup_db")


async def test_build_canonical_media_export_returns_expected_shape():
    await db.upsert_scan_job(
        plex_guid="guid-export",
        title="Export Movie",
        file_path="/tmp/export.mkv",
        rating_key="42",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-export",
        "Export Movie",
        start_ms=1000,
        end_ms=4000,
        category="profanity",
        source="subtitles",
        confidence=0.91,
        text_excerpt="example snippet",
    )

    payload = (await build_canonical_media_export("guid-export")).to_record()

    assert payload["format"] == "leapfrog.segment.export/v1"
    assert payload["media_id"] == "guid-export"
    assert payload["title"] == "Export Movie"
    assert payload["external_ids"]["plex_guid"] == "guid-export"
    assert payload["external_ids"]["plex_rating_key"] == "42"
    assert payload["segment_count"] == 1
    assert payload["segments"][0]["category"] == "profanity"
    assert payload["segments"][0]["source"] == "subtitles"
    assert payload["segments"][0]["text_excerpt"] == "example snippet"


async def test_build_canonical_media_export_filters_by_category_and_confidence():
    await db.upsert_scan_job(
        plex_guid="guid-filtered-export",
        title="Filtered Export Movie",
        file_path="/tmp/filter.mkv",
        rating_key="43",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-filtered-export",
        "Filtered Export Movie",
        start_ms=1000,
        end_ms=3000,
        category="nudity",
        source="nudenet",
        confidence=0.95,
    )
    await db.insert_segment(
        "guid-filtered-export",
        "Filtered Export Movie",
        start_ms=5000,
        end_ms=7000,
        category="profanity",
        source="subtitles",
        confidence=0.2,
    )

    payload = await build_canonical_media_export(
        "guid-filtered-export",
        categories={"nudity"},
        min_confidence=0.5,
    )

    assert len(payload.segments) == 1
    assert payload.segments[0].category == "nudity"


async def test_build_canonical_media_export_filters_for_user_preferences():
    await db.upsert_scan_job(
        plex_guid="guid-user-export",
        title="User Export Movie",
        file_path="/tmp/user.mkv",
        rating_key="44",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-user-export",
        "User Export Movie",
        start_ms=1000,
        end_ms=3000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )
    await db.insert_segment(
        "guid-user-export",
        "User Export Movie",
        start_ms=5000,
        end_ms=7000,
        category="profanity",
        source="subtitles",
        confidence=0.9,
        text_excerpt="flagged phrase",
    )
    await db.set_user_preference("alice", "nudity", enabled=False, threshold=0.5)
    await db.set_user_preference("alice", "profanity", enabled=True, threshold=0.5)

    payload = await build_canonical_media_export("guid-user-export", user_id="alice")

    assert len(payload.segments) == 1
    assert payload.segments[0].category == "profanity"
    assert payload.segments[0].text_excerpt == "flagged phrase"


async def test_sidecar_payload_round_trips_through_file(tmp_path):
    await db.upsert_scan_job(
        plex_guid="guid-sidecar",
        title="Sidecar Movie",
        file_path="/tmp/sidecar.mkv",
        rating_key="45",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-sidecar",
        "Sidecar Movie",
        start_ms=2000,
        end_ms=3500,
        category="profanity",
        source="subtitles",
        confidence=0.88,
        labels="damn",
        text_excerpt="sidecar excerpt",
    )

    payload = await build_sidecar_payload("guid-sidecar")
    path = await write_sidecar_file(tmp_path / "guid-sidecar.json", payload)
    restored = await read_sidecar_file(path)

    assert restored["format"] == "leapfrog.segment.sidecar/v1"
    assert restored["media_id"] == "guid-sidecar"
    assert restored["segments"][0]["confidence"] == pytest.approx(0.88)
    assert restored["segments"][0]["labels"] == "damn"
    assert restored["segments"][0]["text_excerpt"] == "sidecar excerpt"


async def test_adapters_return_target_specific_lookup_metadata():
    await db.upsert_scan_job(
        plex_guid="guid-adapter",
        title="Adapter Movie",
        file_path="/tmp/adapter.mkv",
        rating_key="46",
        library_id="lib1",
        library_title="Movies",
    )

    media_export = await build_canonical_media_export("guid-adapter")
    plex_payload = get_segment_adapter("plex").adapt(media_export)
    emby_payload = get_segment_adapter("emby").adapt(media_export)
    jellyfin_payload = get_segment_adapter("jellyfin").adapt(media_export)

    assert plex_payload["lookup"]["plex_guid"] == "guid-adapter"
    assert plex_payload["lookup"]["plex_rating_key"] == "46"
    assert emby_payload["lookup"]["provider_id"] == "guid-adapter"
    assert jellyfin_payload["lookup"]["item_id"] == "guid-adapter"
