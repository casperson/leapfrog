"""Unit tests for the Plex runtime playback adapter."""

from __future__ import annotations

import pytest

from leapfrog import database as db
from leapfrog.adapters.plex_runtime import (
    find_sidecar_path,
    map_plex_user_to_user_id,
    resolve_plex_playback_context,
)
from leapfrog.plex_client import ActiveSession
from leapfrog.segment_export import write_sidecar_file


pytestmark = pytest.mark.usefixtures("setup_db")


def _session(
    *,
    user: str = "alice",
    plex_guid: str = "guid-1",
    rating_key: str = "100",
    file_path: str = "",
) -> ActiveSession:
    return ActiveSession(
        session_key="sess-1",
        user=user,
        title="Movie",
        full_title="Movie",
        plex_guid=plex_guid,
        rating_key=rating_key,
        media_type="movie",
        position_ms=1000,
        duration_ms=100000,
        client_identifier="client-1",
        client_title="Plex Web",
        is_controllable=True,
        file_path=file_path,
    )


async def test_resolve_plex_playback_context_uses_rating_key_to_resolve_media_id():
    await db.upsert_scan_job(
        plex_guid="guid-db",
        title="Movie",
        file_path="/tmp/movie.mkv",
        rating_key="rk-1",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-db",
        "Movie",
        start_ms=1000,
        end_ms=3000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )

    context = await resolve_plex_playback_context(
        _session(plex_guid="mismatch-guid", rating_key="rk-1")
    )

    assert context.media_id == "guid-db"
    assert context.external_ids["plex_rating_key"] == "rk-1"
    assert context.segment_source == "db"


async def test_resolve_plex_playback_context_uses_current_plex_user_preferences():
    await db.upsert_scan_job(
        plex_guid="guid-pref",
        title="Movie",
        file_path="/tmp/movie.mkv",
        rating_key="rk-pref",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-pref",
        "Movie",
        start_ms=1000,
        end_ms=3000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )
    await db.insert_segment(
        "guid-pref",
        "Movie",
        start_ms=4000,
        end_ms=6000,
        category="profanity",
        source="subtitles",
        confidence=0.9,
    )
    await db.set_user_preference("alice", "nudity", enabled=False, threshold=0.5)
    await db.set_user_preference("alice", "profanity", enabled=True, threshold=0.5)

    context = await resolve_plex_playback_context(
        _session(plex_guid="guid-pref", rating_key="rk-pref")
    )

    assert context.user_id == "alice"
    assert context.effective_segment_count == 1
    assert context.effective_segments[0]["category"] == "profanity"


async def test_resolve_plex_playback_context_uses_sidecar_when_available(tmp_path):
    media_path = tmp_path / "Movie.mkv"
    media_path.write_text("stub", encoding="utf-8")
    sidecar_path = media_path.with_suffix(".leapfrog.json")
    await write_sidecar_file(
        sidecar_path,
        {
            "format": "leapfrog.segment.sidecar/v1",
            "media_id": "sidecar-media",
            "title": "Sidecar Movie",
            "segments": [
                {
                    "start_time": 1.0,
                    "end_time": 3.0,
                    "category": "profanity",
                    "source": "subtitles",
                    "confidence": 0.8,
                    "labels": "damn",
                    "text_excerpt": "sidecar line",
                }
            ],
        },
    )

    context = await resolve_plex_playback_context(
        _session(
            user="alice",
            plex_guid="guid-sidecar",
            rating_key="rk-sidecar",
            file_path=str(media_path),
        ),
        user_preferences={"profanity": {"enabled": True, "threshold": 0.5}},
    )

    assert context.segment_source == "sidecar"
    assert context.sidecar_path == str(sidecar_path)
    assert context.effective_segment_count == 1
    assert context.effective_segments[0]["text_excerpt"] == "sidecar line"
    assert context.effective_segments[0]["labels"] == "damn"


async def test_resolve_plex_playback_context_respects_sidecar_label_preferences(tmp_path):
    media_path = tmp_path / "Movie.mkv"
    media_path.write_text("stub", encoding="utf-8")
    await write_sidecar_file(
        media_path.with_suffix(".segments.json"),
        {
            "format": "leapfrog.segment.sidecar/v1",
            "media_id": "sidecar-media",
            "segments": [
                {
                    "start_time": 1.0,
                    "end_time": 3.0,
                    "category": "sexual_content",
                    "source": "sidecar",
                    "confidence": 0.9,
                    "labels": "brief_kiss",
                },
                {
                    "start_time": 4.0,
                    "end_time": 6.0,
                    "category": "sexual_content",
                    "source": "sidecar",
                    "confidence": 0.9,
                    "labels": "explicit_sex",
                },
            ],
        },
    )

    context = await resolve_plex_playback_context(
        _session(file_path=str(media_path)),
        user_preferences={
            "sexual_content": {
                "enabled": True,
                "threshold": 0.5,
                "labels": {
                    "brief_kiss": {"enabled": False, "threshold": None},
                    "explicit_sex": {"enabled": True, "threshold": None},
                },
            },
        },
    )

    assert context.effective_segment_count == 1
    assert context.effective_segments[0]["labels"] == "explicit_sex"


async def test_resolve_plex_playback_context_reads_adjacent_edl_file(tmp_path):
    media_path = tmp_path / "Movie.mkv"
    media_path.write_text("stub", encoding="utf-8")
    media_path.with_suffix(".edl").write_text(
        "10.0 20.5 0 violence fight\n",
        encoding="utf-8",
    )

    context = await resolve_plex_playback_context(
        _session(file_path=str(media_path)),
        user_preferences={"violence": {"enabled": True, "threshold": 0.5}},
    )

    assert context.segment_source == "sidecar"
    assert context.effective_segments[0]["start_ms"] == 10000
    assert context.effective_segments[0]["end_ms"] == 20500
    assert context.effective_segments[0]["category"] == "violence"
    assert context.effective_segments[0]["labels"] == "fight"


async def test_resolve_plex_playback_context_reads_adjacent_csv_file(tmp_path):
    media_path = tmp_path / "Movie.mkv"
    media_path.write_text("stub", encoding="utf-8")
    media_path.with_suffix(".csv").write_text(
        "start_time,end_time,category,labels,confidence\n"
        "00:01:00,00:01:10,drugs,marijuana,0.91\n",
        encoding="utf-8",
    )

    context = await resolve_plex_playback_context(
        _session(file_path=str(media_path)),
        user_preferences={
            "drugs": {
                "enabled": True,
                "threshold": 0.5,
                "labels": {"marijuana": {"enabled": True, "threshold": None}},
            },
        },
    )

    assert context.segment_source == "sidecar"
    assert context.effective_segments[0]["start_ms"] == 60000
    assert context.effective_segments[0]["category"] == "drugs"


async def test_resolve_plex_playback_context_falls_back_to_db_without_sidecar():
    await db.upsert_scan_job(
        plex_guid="guid-fallback",
        title="Movie",
        file_path="/tmp/fallback.mkv",
        rating_key="rk-fallback",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-fallback",
        "Movie",
        start_ms=1000,
        end_ms=3000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )

    context = await resolve_plex_playback_context(
        _session(plex_guid="guid-fallback", rating_key="rk-fallback")
    )

    assert context.segment_source == "db"
    assert context.effective_segment_count == 1


async def test_resolve_plex_playback_context_defaults_to_nudity_when_preferences_missing():
    await db.upsert_scan_job(
        plex_guid="guid-defaults",
        title="Movie",
        file_path="/tmp/defaults.mkv",
        rating_key="rk-defaults",
        library_id="lib1",
        library_title="Movies",
    )
    await db.insert_segment(
        "guid-defaults",
        "Movie",
        start_ms=1000,
        end_ms=3000,
        category="nudity",
        source="nudenet",
        confidence=0.9,
    )
    await db.insert_segment(
        "guid-defaults",
        "Movie",
        start_ms=4000,
        end_ms=6000,
        category="profanity",
        source="subtitles",
        confidence=0.9,
    )

    context = await resolve_plex_playback_context(
        _session(user="new-user", plex_guid="guid-defaults", rating_key="rk-defaults")
    )

    assert context.preferences_resolved is True
    assert context.effective_segment_count == 1
    assert context.effective_segments[0]["category"] == "nudity"


async def test_find_sidecar_path_checks_adjacent_export_names(tmp_path):
    media_path = tmp_path / "Movie.mkv"
    media_path.write_text("stub", encoding="utf-8")
    sidecar_path = media_path.with_suffix(".segments.json")
    sidecar_path.write_text("{}", encoding="utf-8")

    resolved = await find_sidecar_path(str(media_path))

    assert resolved == sidecar_path


def test_map_plex_user_to_user_id_returns_trimmed_username():
    assert map_plex_user_to_user_id(" alice ") == "alice"
