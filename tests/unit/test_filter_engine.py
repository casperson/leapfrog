"""Unit tests for filter_engine.py — seek decision logic."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import leapfrog.filter_engine as fe
from leapfrog.adapters.plex_runtime import PlexPlaybackContext
from leapfrog.plex_client import ActiveSession


def _session(
    *,
    session_key: str = "sess-1",
    plex_guid: str = "guid-1",
    rating_key: str = "rk-1",
    position_ms: int = 0,
    is_controllable: bool = True,
    user: str = "alice",
    client_identifier: str = "client-abc",
    client_address: str = "192.168.1.10",
    client_port: int = 32500,
) -> ActiveSession:
    return ActiveSession(
        session_key=session_key,
        user=user,
        title="Movie",
        full_title="Movie",
        plex_guid=plex_guid,
        rating_key=rating_key,
        media_type="movie",
        position_ms=position_ms,
        duration_ms=7200000,
        client_identifier=client_identifier,
        client_title="Plex Web",
        is_controllable=is_controllable,
        client_address=client_address,
        client_port=client_port,
    )


def _make_client(seek_result: bool = True) -> MagicMock:
    client = MagicMock()
    client.seek = AsyncMock(return_value=seek_result)
    return client


def _segs(start: int, end: int, *, category: str = "nudity", confidence: float = 0.9) -> list[dict]:
    return [
        {
            "start_ms": start,
            "end_ms": end,
            "confidence": confidence,
            "category": category,
            "source": "db",
        }
    ]


def _playback_context(
    *,
    all_segments: list[dict] | None = None,
    effective_segments: list[dict] | None = None,
    segment_source: str = "db",
) -> PlexPlaybackContext:
    return PlexPlaybackContext(
        adapter="plex",
        connected=True,
        media_resolved=bool(all_segments),
        media_id="guid-1",
        title="Movie",
        user_id="alice",
        preferences_resolved=True,
        segment_source=segment_source,
        all_segments=all_segments or [],
        effective_segments=effective_segments or [],
        external_ids={"plex_guid": "guid-1", "plex_rating_key": "rk-1"},
        scan_statuses=[],
    )


@pytest.fixture(autouse=True)
def reset_filter_state():
    """Clear global filter state before each test to prevent cross-test bleed."""
    fe._recently_skipped.clear()
    fe._seek_backoff_until.clear()
    yield
    fe._recently_skipped.clear()
    fe._seek_backoff_until.clear()


async def test_non_controllable_session_skips_without_seek():
    session = _session(is_controllable=False)
    client = _make_client()
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context()),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    client.seek.assert_not_called()


async def test_no_segments_does_not_seek():
    session = _session(position_ms=5000)
    client = _make_client()
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context()),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    client.seek.assert_not_called()


async def test_runtime_adapter_segments_can_trigger_seek():
    session = _session(position_ms=50000, rating_key="rk-fallback")
    client = _make_client()
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(
            return_value=_playback_context(
                all_segments=_segs(45000, 60000),
                effective_segments=_segs(45000, 60000),
            )
        ),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    client.seek.assert_awaited_once()


async def test_position_within_lookahead_triggers_seek():
    session = _session(position_ms=21000)
    client = _make_client()
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000, lookahead_ms=5000)

    client.seek.assert_awaited_once()
    _, seek_ms, *_ = client.seek.call_args[0]
    assert seek_ms == 43000


async def test_position_before_lookahead_does_not_seek():
    session = _session(position_ms=5000)
    client = _make_client()
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000, lookahead_ms=5000)
    client.seek.assert_not_called()


async def test_position_inside_segment_triggers_seek():
    session = _session(position_ms=35000)
    client = _make_client()
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000, lookahead_ms=5000)
    client.seek.assert_awaited_once()


async def test_position_past_segment_does_not_seek():
    session = _session(position_ms=55000)
    client = _make_client()
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000, lookahead_ms=5000)
    client.seek.assert_not_called()


async def test_recently_skipped_prevents_re_trigger():
    session = _session(position_ms=35000)
    client = _make_client()
    fe._recently_skipped["sess-1"] = {"key": "guid-1|30000|40000|nudity|db|", "until_ms": 50000}
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    client.seek.assert_not_called()


async def test_recently_skipped_cleared_when_past_end():
    session = _session(position_ms=60000)
    client = _make_client()
    fe._recently_skipped["sess-1"] = {"key": "guid-1|30000|40000|nudity|db|", "until_ms": 50000}
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    assert "sess-1" not in fe._recently_skipped


async def test_seek_backoff_prevents_retry():
    session = _session(position_ms=35000)
    client = _make_client()
    fe._seek_backoff_until["sess-1"] = time.time() + 60
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    client.seek.assert_not_called()


async def test_failed_seek_sets_backoff():
    session = _session(position_ms=35000)
    client = _make_client(seek_result=False)
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    assert "sess-1" in fe._seek_backoff_until
    assert fe._seek_backoff_until["sess-1"] > time.time()


async def test_successful_seek_records_recently_skipped():
    session = _session(position_ms=35000)
    client = _make_client(seek_result=True)
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    assert fe._recently_skipped["sess-1"]["until_ms"] == 45000
    assert fe._recently_skipped["sess-1"]["key"] == "guid-1|30000|40000|nudity|db|"


async def test_successful_seek_clears_backoff():
    session = _session(position_ms=35000)
    client = _make_client(seek_result=True)
    fe._seek_backoff_until["sess-1"] = time.time() - 1
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    assert "sess-1" not in fe._seek_backoff_until


async def test_disabled_category_does_not_seek():
    session = _session(position_ms=35000)
    client = _make_client()
    segments = _segs(30000, 40000)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=[])),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    client.seek.assert_not_called()


async def test_thresholded_category_below_threshold_does_not_seek():
    session = _session(position_ms=35000)
    client = _make_client()
    segments = _segs(30000, 40000, category="profanity", confidence=0.4)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=segments, effective_segments=[])),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    client.seek.assert_not_called()


async def test_nudity_only_preferences_skip_only_nudity_segments():
    session = _session(position_ms=26000)
    client = _make_client()
    all_segments = [
        *_segs(30000, 40000, category="nudity"),
        *_segs(10000, 15000, category="profanity", confidence=0.95),
    ]
    effective_segments = _segs(30000, 40000, category="nudity")
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=all_segments, effective_segments=effective_segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)

    client.seek.assert_awaited_once()
    _, seek_ms, *_ = client.seek.call_args[0]
    assert seek_ms == 43000


async def test_profanity_only_preferences_skip_only_profanity_segments():
    session = _session(position_ms=6000)
    client = _make_client()
    all_segments = [
        *_segs(30000, 40000, category="nudity"),
        *_segs(10000, 15000, category="profanity", confidence=0.95),
    ]
    effective_segments = _segs(10000, 15000, category="profanity", confidence=0.95)
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=all_segments, effective_segments=effective_segments)),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)

    client.seek.assert_awaited_once()
    _, seek_ms, *_ = client.seek.call_args[0]
    assert seek_ms == 18000


async def test_all_categories_disabled_skips_nothing():
    session = _session(position_ms=26000)
    client = _make_client()
    all_segments = [
        *_segs(30000, 40000, category="nudity"),
        *_segs(10000, 15000, category="profanity", confidence=0.95),
    ]
    with patch(
        "leapfrog.filter_engine.resolve_plex_playback_context",
        AsyncMock(return_value=_playback_context(all_segments=all_segments, effective_segments=[])),
    ):
        await fe.process(session, client, skip_buffer_ms=3000)
    client.seek.assert_not_called()
