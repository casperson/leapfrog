"""Unit tests for Emby and Jellyfin runtime adapters."""

from __future__ import annotations

import pytest

from leapfrog import database as db
from leapfrog.adapters import get_runtime_adapter
from leapfrog.adapters.emby_runtime import (
    EmbyPlaybackReference,
    build_emby_media_reference,
    resolve_emby_playback_context,
)
from leapfrog.adapters.jellyfin_runtime import (
    JellyfinPlaybackReference,
    build_jellyfin_media_reference,
    resolve_jellyfin_playback_context,
)
from leapfrog.segment_export import write_sidecar_file


pytestmark = pytest.mark.usefixtures("setup_db")


async def test_emby_runtime_adapter_resolves_media_by_canonical_media_id():
    await db.upsert_scan_job(
        plex_guid="guid-emby",
        title="Emby Movie",
        file_path="/tmp/emby.mkv",
        rating_key="rk-emby",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-emby",
        "Emby Movie",
        start_ms=1000,
        end_ms=3000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )

    context = await resolve_emby_playback_context(
        EmbyPlaybackReference(
            user_name="alice",
            item_id="emby-item-1",
            title="Emby Movie",
            canonical_media_id="guid-emby",
        )
    )

    assert context.adapter == "emby"
    assert context.media_resolved is True
    assert context.media_id == "guid-emby"
    assert context.segment_source == "db"


async def test_emby_runtime_adapter_uses_sidecar_when_available(tmp_path):
    media_path = tmp_path / "EmbyMovie.mkv"
    media_path.write_text("stub", encoding="utf-8")
    sidecar_path = media_path.with_suffix(".leapfrog.json")
    await write_sidecar_file(
        sidecar_path,
        {
            "format": "leapfrog.segment.sidecar/v1",
            "media_id": "emby-sidecar-media",
            "title": "Emby Sidecar Movie",
            "segments": [
                {
                    "start_time": 2.0,
                    "end_time": 4.0,
                    "category": "profanity",
                    "source": "subtitles",
                    "confidence": 0.8,
                    "text_excerpt": "emby line",
                }
            ],
        },
    )

    context = await resolve_emby_playback_context(
        EmbyPlaybackReference(
            user_name="alice",
            item_id="emby-item-sidecar",
            title="Emby Sidecar Movie",
            file_path=str(media_path),
        ),
        user_preferences={"profanity": {"enabled": True, "threshold": 0.5}},
    )

    assert context.media_resolved is True
    assert context.media_id == "emby-sidecar-media"
    assert context.segment_source == "sidecar"
    assert context.effective_segment_count == 1
    assert context.effective_segments[0]["text_excerpt"] == "emby line"


async def test_emby_runtime_adapter_respects_preferences():
    await db.upsert_scan_job(
        plex_guid="guid-emby-pref",
        title="Emby Pref Movie",
        file_path="/tmp/emby-pref.mkv",
        rating_key="rk-emby-pref",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-emby-pref",
        "Emby Pref Movie",
        start_ms=1000,
        end_ms=3000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )
    await db.insert_segment(
        "guid-emby-pref",
        "Emby Pref Movie",
        start_ms=5000,
        end_ms=7000,
        category="profanity",
        source="subtitles",
        confidence=0.9,
    )
    await db.set_user_preference("alice", "nudity", enabled=False, threshold=0.5)
    await db.set_user_preference("alice", "profanity", enabled=True, threshold=0.5)

    context = await resolve_emby_playback_context(
        EmbyPlaybackReference(
            user_name="alice",
            item_id="emby-item-pref",
            title="Emby Pref Movie",
            canonical_media_id="guid-emby-pref",
        )
    )

    assert context.effective_segment_count == 1
    assert context.effective_segments[0]["category"] == "profanity"


async def test_jellyfin_runtime_adapter_resolves_media_by_file_path_fallback():
    await db.upsert_scan_job(
        plex_guid="guid-jellyfin",
        title="Jellyfin Movie",
        file_path="/tmp/jellyfin.mkv",
        rating_key="rk-jellyfin",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-jellyfin",
        "Jellyfin Movie",
        start_ms=1000,
        end_ms=3000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )

    context = await resolve_jellyfin_playback_context(
        JellyfinPlaybackReference(
            user_name="bob",
            item_id="jellyfin-item-1",
            title="Jellyfin Movie",
            file_path="/tmp/jellyfin.mkv",
        )
    )

    assert context.adapter == "jellyfin"
    assert context.media_resolved is True
    assert context.media_id == "guid-jellyfin"
    assert context.segment_source == "db"


async def test_jellyfin_runtime_adapter_defaults_to_nudity_when_preferences_missing():
    await db.upsert_scan_job(
        plex_guid="guid-jelly-default",
        title="Jellyfin Default Movie",
        file_path="/tmp/jelly-default.mkv",
        rating_key="rk-jelly-default",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-jelly-default",
        "Jellyfin Default Movie",
        start_ms=1000,
        end_ms=3000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )
    await db.insert_segment(
        "guid-jelly-default",
        "Jellyfin Default Movie",
        start_ms=5000,
        end_ms=7000,
        category="profanity",
        source="subtitles",
        confidence=0.9,
    )

    context = await resolve_jellyfin_playback_context(
        JellyfinPlaybackReference(
            user_name="new-user",
            item_id="jellyfin-item-default",
            title="Jellyfin Default Movie",
            canonical_media_id="guid-jelly-default",
        )
    )

    assert context.preferences_resolved is True
    assert context.effective_segment_count == 1
    assert context.effective_segments[0]["category"] == "nudity"


async def test_jellyfin_runtime_adapter_respects_confidence_thresholds():
    await db.upsert_scan_job(
        plex_guid="guid-jelly-threshold",
        title="Jellyfin Threshold Movie",
        file_path="/tmp/jelly-threshold.mkv",
        rating_key="rk-jelly-threshold",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-jelly-threshold",
        "Jellyfin Threshold Movie",
        start_ms=1000,
        end_ms=3000,
        category="profanity",
        source="subtitles",
        confidence=0.4,
    )

    context = await resolve_jellyfin_playback_context(
        JellyfinPlaybackReference(
            user_name="bob",
            item_id="jellyfin-item-threshold",
            title="Jellyfin Threshold Movie",
            canonical_media_id="guid-jelly-threshold",
        ),
        user_preferences={"profanity": {"enabled": True, "threshold": 0.5}},
    )

    assert context.media_resolved is True
    assert context.effective_segment_count == 0
    assert context.segment_source == "db"


async def test_jellyfin_runtime_adapter_returns_clear_unresolved_status(tmp_path):
    media_path = tmp_path / "UnknownMovie.mkv"
    media_path.write_text("stub", encoding="utf-8")

    context = await resolve_jellyfin_playback_context(
        JellyfinPlaybackReference(
            user_name="bob",
            item_id="jellyfin-unknown",
            title="Unknown Movie",
            file_path=str(media_path),
        )
    )

    assert context.media_resolved is False
    assert context.segment_source == "none"
    assert context.effective_segment_count == 0


async def test_runtime_adapter_registry_returns_emby_and_jellyfin():
    emby_adapter = get_runtime_adapter("emby")
    jellyfin_adapter = get_runtime_adapter("jellyfin")

    assert emby_adapter.name == "emby"
    assert jellyfin_adapter.name == "jellyfin"

    emby_reference = build_emby_media_reference(
        EmbyPlaybackReference(
            user_name="alice",
            item_id="emby-item",
            title="Emby Movie",
            canonical_media_id="guid-adapter",
        )
    )
    jellyfin_reference = build_jellyfin_media_reference(
        JellyfinPlaybackReference(
            user_name="bob",
            item_id="jellyfin-item",
            title="Jellyfin Movie",
            canonical_media_id="guid-adapter",
        )
    )

    assert emby_reference.adapter == "emby"
    assert jellyfin_reference.adapter == "jellyfin"
