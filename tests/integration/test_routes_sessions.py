"""Integration tests for sessions API routes."""

from __future__ import annotations

import collections
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leapfrog import database as db
from leapfrog.plex_client import ActiveSession
from tests.conftest import make_mock_plex_client


pytestmark = pytest.mark.usefixtures("setup_db")


def _active_session(
    *,
    session_key: str = "s1",
    user: str = "alice",
    title: str = "Movie",
    plex_guid: str = "g1",
    is_controllable: bool = True,
    position_ms: int = 5000,
    thumb: str = "",
) -> ActiveSession:
    return ActiveSession(
        session_key=session_key,
        user=user,
        title=title,
        full_title=title,
        plex_guid=plex_guid,
        rating_key="100",
        media_type="movie",
        position_ms=position_ms,
        duration_ms=7200000,
        client_identifier="client-1",
        client_title="Plex Web",
        is_controllable=is_controllable,
        thumb=thumb,
    )


# ── GET /api/sessions ─────────────────────────────────────────────────────────

async def test_get_sessions_returns_empty_when_no_plex(http_client):
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", side_effect=RuntimeError("not set")):
        resp = await http_client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessions"] == []
    assert "error" in data


async def test_get_sessions_returns_session_list(http_client):
    sessions = [_active_session()]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions")
    assert resp.status_code == 200
    result = resp.json()["sessions"]
    assert len(result) == 1
    assert result[0]["session_key"] == "s1"
    assert result[0]["user"] == "alice"


async def test_get_sessions_filtering_enabled_default_when_no_filter(http_client):
    """Category toggles default off until a user explicitly enables them."""
    sessions = [_active_session(user="bob")]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions")
    result = resp.json()["sessions"]
    assert result[0]["filtering_enabled"] is False
    assert result[0]["enabled_categories"] == []


async def test_get_sessions_filtering_disabled_when_filter_set(http_client):
    await db.upsert_user_filter("charlie", enabled=False)
    sessions = [_active_session(user="charlie")]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions")
    result = resp.json()["sessions"]
    assert result[0]["filtering_enabled"] is False


async def test_get_sessions_batches_user_filter_lookup(http_client):
    """All user filter lookups happen in one batch call, not N per session."""
    sessions = [_active_session(session_key=f"s{i}", user=f"user{i}") for i in range(5)]
    mock_client = make_mock_plex_client(sessions=sessions)

    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client), \
         patch("leapfrog.web.routes.sessions.db.get_all_user_filters", wraps=db.get_all_user_filters) as spy:
        resp = await http_client.get("/api/sessions")

    assert resp.status_code == 200
    # Must call get_all_user_filters exactly once regardless of session count
    spy.assert_awaited_once()


# ── GET /api/sessions/events ──────────────────────────────────────────────────

async def test_get_skip_events_returns_empty_by_default(http_client):
    with patch("leapfrog.web.routes.sessions.skip_events", collections.deque()):
        resp = await http_client.get("/api/sessions/events")
    assert resp.status_code == 200
    assert resp.json()["events"] == []


async def test_get_skip_events_returns_events(http_client):
    events = [{"time": "2025-01-01 10:00:00", "user": "alice", "title": "Movie",
               "position_ms": 5000, "client": "Web"}]
    with patch("leapfrog.web.routes.sessions.skip_events", collections.deque(events)):
        resp = await http_client.get("/api/sessions/events")
    result = resp.json()["events"]
    assert len(result) == 1
    assert result[0]["user"] == "alice"


async def test_get_session_adapter_status_returns_status(http_client):
    await db.insert_segment(
        "g1",
        "Movie",
        start_ms=30000,
        end_ms=60000,
        confidence=0.9,
        category="sex_nudity_immodesty",
    )
    await db.set_user_preference("alice", "sex_nudity_immodesty", enabled=True, threshold=0.5)
    sessions = [_active_session()]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions/s1/adapter-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["adapter"] == "plex"
    assert data["connected"] is True
    assert data["segment_source"] == "db"
    assert data["effective_segment_count"] == 1


async def test_get_session_seek_diagnostics_returns_last_attempts(http_client):
    sessions = [_active_session()]
    diagnostics = {
        "started_at": "2026-04-25T09:00:00",
        "client_identifier": "client-1",
        "offset_ms": 63000,
        "success": False,
        "attempts": [
            {"method": "proxy", "ok": False, "detail": "proxy failed"},
            {"method": "direct", "ok": False, "detail": "connection refused", "client_port": 32500},
        ],
    }
    mock_client = make_mock_plex_client(sessions=sessions)
    mock_client.get_last_seek_failure.return_value = {"detail": "connection refused"}
    mock_client.get_last_seek_diagnostics.return_value = diagnostics

    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions/s1/seek-diagnostics")

    assert resp.status_code == 200
    data = resp.json()
    assert data["session"]["client_identifier"] == "client-1"
    assert data["last_seek_failure"]["detail"] == "connection refused"
    assert data["last_seek_diagnostics"]["attempts"][1]["client_port"] == 32500


async def test_get_session_seek_diagnostics_returns_404_for_missing_session(http_client):
    mock_client = make_mock_plex_client(sessions=[])
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions/missing/seek-diagnostics")

    assert resp.status_code == 404


async def test_get_companion_clients_returns_discovered_client_list(http_client):
    mock_client = make_mock_plex_client()
    mock_client.list_companion_clients = AsyncMock(return_value=[
        {
            "title": "iPhone",
            "machine_identifier": "client-1",
            "product": "Plex for iOS",
            "protocol_capabilities": ["playback", "timeline"],
            "baseurl": "http://192.168.1.104:32500",
        }
    ])

    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions/companion-clients")

    assert resp.status_code == 200
    data = resp.json()
    assert data["clients"][0]["machine_identifier"] == "client-1"
    mock_client.list_companion_clients.assert_awaited_once()


async def test_get_session_companion_compare_returns_session_and_match(http_client):
    sessions = [_active_session(session_key="s1")]
    mock_client = make_mock_plex_client(sessions=sessions)
    mock_client.compare_session_to_companion = AsyncMock(return_value={
        "session": {"session_key": "s1", "client_identifier": "client-1"},
        "matched_companion_client": {"machine_identifier": "client-1", "baseurl": "http://192.168.1.100:32500"},
        "companion_clients": [{"machine_identifier": "client-1"}],
    })

    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.get("/api/sessions/s1/companion-compare")

    assert resp.status_code == 200
    data = resp.json()
    assert data["matched_companion_client"]["machine_identifier"] == "client-1"
    mock_client.compare_session_to_companion.assert_awaited_once()


async def test_probe_session_seek_returns_probe_payload(http_client):
    sessions = [_active_session(session_key="s1", position_ms=5000)]
    mock_client = make_mock_plex_client(sessions=sessions)
    mock_client.probe_seek = AsyncMock(
        return_value={
            "session": {"session_key": "s1"},
            "probe_success": False,
            "requested_offset_ms": 6500,
            "last_seek_diagnostics": {"attempts": [{"method": "proxy"}]},
        }
    )

    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/probe-seek", json={"delta_ms": 1500})

    assert resp.status_code == 200
    data = resp.json()
    assert data["requested_offset_ms"] == 6500
    mock_client.probe_seek.assert_awaited_once()


async def test_probe_session_seek_returns_404_for_missing_session(http_client):
    mock_client = make_mock_plex_client(sessions=[])
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/missing/probe-seek", json={"delta_ms": 1000})

    assert resp.status_code == 404


# ── GET /api/sessions/scanner-status ─────────────────────────────────────────

async def test_scanner_status_returns_expected_shape(http_client):
    with patch("leapfrog.web.routes.sessions.get_current_scan", return_value=None), \
         patch("leapfrog.web.routes.sessions.get_current_scans", return_value=[]), \
         patch("leapfrog.web.routes.sessions.get_queue_size", return_value=0), \
         patch("leapfrog.web.routes.sessions.get_worker_pool_size", return_value=2), \
         patch("leapfrog.web.routes.sessions.is_paused", return_value=False):
        resp = await http_client.get("/api/sessions/scanner-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "queue_size" in data
    assert "active_scans" in data
    assert "paused" in data
    assert "skipper" in data
    assert data["paused"] is False


async def test_scanner_status_marks_skipper_degraded_after_seek_failure(http_client):
    mock_client = make_mock_plex_client()
    mock_client.get_last_seek_failure.return_value = {
        "at": "2026-04-25T08:00:00",
        "method": "direct",
        "detail": "Connection refused",
    }
    mock_client.get_last_seek_success_at.return_value = None

    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client), \
         patch("leapfrog.web.routes.sessions.get_current_scan", return_value=None), \
         patch("leapfrog.web.routes.sessions.get_current_scans", return_value=[]), \
         patch("leapfrog.web.routes.sessions.get_queue_size", return_value=0), \
         patch("leapfrog.web.routes.sessions.get_worker_pool_size", return_value=2), \
         patch("leapfrog.web.routes.sessions.is_paused", return_value=False), \
         patch(
             "leapfrog.web.routes.sessions.get_skipper_runtime_state",
             return_value={
                 "last_poll_at": "2026-04-25T08:00:01",
                 "last_success_at": "2026-04-25T08:00:01",
                 "last_error": None,
                 "last_error_at": None,
                 "last_skip_at": None,
                 "last_session_count": 1,
             },
         ):
        resp = await http_client.get("/api/sessions/scanner-status")

    assert resp.status_code == 200
    skipper = resp.json()["skipper"]
    assert skipper["healthy"] is False
    assert skipper["status"] == "degraded"
    assert skipper["last_seek_failure"]["detail"] == "Connection refused"


async def test_scanner_status_marks_skipper_idle_when_no_sessions_after_seek_failure(http_client):
    mock_client = make_mock_plex_client()
    mock_client.get_last_seek_failure.return_value = {
        "at": "2026-04-25T08:00:00",
        "method": "direct",
        "detail": "Connection refused",
    }
    mock_client.get_last_seek_success_at.return_value = None

    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client), \
         patch("leapfrog.web.routes.sessions.get_current_scan", return_value=None), \
         patch("leapfrog.web.routes.sessions.get_current_scans", return_value=[]), \
         patch("leapfrog.web.routes.sessions.get_queue_size", return_value=0), \
         patch("leapfrog.web.routes.sessions.get_worker_pool_size", return_value=2), \
         patch("leapfrog.web.routes.sessions.is_paused", return_value=False), \
         patch(
             "leapfrog.web.routes.sessions.get_skipper_runtime_state",
             return_value={
                 "last_poll_at": "2026-04-25T08:00:01",
                 "last_success_at": "2026-04-25T08:00:01",
                 "last_error": None,
                 "last_error_at": None,
                 "last_skip_at": None,
                 "last_session_count": 0,
             },
         ):
        resp = await http_client.get("/api/sessions/scanner-status")

    assert resp.status_code == 200
    skipper = resp.json()["skipper"]
    assert skipper["healthy"] is True
    assert skipper["status"] == "idle"
    assert skipper["last_seek_failure"]["detail"] == "Connection refused"


async def test_scanner_status_batches_db_lookup(http_client):
    """scanner-status must call get_scan_jobs_by_guids (batch), not per-guid."""
    guid = "scan-guid-1"
    await db.upsert_scan_job(
        plex_guid=guid, title="Scanning Title", file_path="/f.mkv",
        rating_key="1", library_id="1", library_title="L",
    )
    await db.update_scan_job_status(guid, "scanning", progress=0.5)

    with patch("leapfrog.web.routes.sessions.get_current_scan", return_value=guid), \
         patch("leapfrog.web.routes.sessions.get_current_scans", return_value=[guid]), \
         patch("leapfrog.web.routes.sessions.get_queue_size", return_value=0), \
         patch("leapfrog.web.routes.sessions.get_worker_pool_size", return_value=2), \
         patch("leapfrog.web.routes.sessions.is_paused", return_value=False), \
         patch("leapfrog.web.routes.sessions.db.get_scan_jobs_by_guids",
               wraps=db.get_scan_jobs_by_guids) as spy:
        resp = await http_client.get("/api/sessions/scanner-status")

    assert resp.status_code == 200
    # Exactly one batch call, not a loop of individual queries
    spy.assert_awaited_once()
    data = resp.json()
    assert len(data["active_scans"]) == 1
    assert data["active_scans"][0]["guid"] == guid


# ── POST /api/sessions/{session_key}/skip ─────────────────────────────────────

async def test_skip_session_returns_404_when_plex_not_configured(http_client):
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", side_effect=RuntimeError):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 503


async def test_skip_session_returns_404_when_session_not_found(http_client):
    mock_client = make_mock_plex_client(sessions=[])
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/no-such-session/skip")
    assert resp.status_code == 404


async def test_skip_session_returns_409_when_not_controllable(http_client):
    sessions = [_active_session(is_controllable=False)]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 409


async def test_skip_session_returns_404_when_no_segments(http_client):
    sessions = [_active_session()]
    mock_client = make_mock_plex_client(sessions=sessions)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 404


async def test_skip_session_seeks_to_next_segment(http_client):
    await db.insert_segment(
        "g1",
        "Movie",
        start_ms=30000,
        end_ms=60000,
        confidence=0.9,
        category="sex_nudity_immodesty",
    )
    await db.set_user_preference("alice", "sex_nudity_immodesty", enabled=True, threshold=0.5)
    sessions = [_active_session(position_ms=10000)]
    mock_client = make_mock_plex_client(sessions=sessions, seek_result=True)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["seek_to_ms"] == 61000
    assert data["adapter_status"]["segment_source"] == "db"


async def test_skip_session_with_nudity_enabled_only_ignores_other_categories(http_client):
    await db.insert_segment("g1", "Movie", start_ms=30000, end_ms=60000, confidence=0.9, category="sex_nudity_immodesty")
    await db.insert_segment("g1", "Movie", start_ms=10000, end_ms=20000, confidence=0.9, category="language_profanity")
    await db.set_user_preference("alice", "sex_nudity_immodesty", enabled=True, threshold=0.5)
    await db.set_user_preference("alice", "language_profanity", enabled=False, threshold=0.5)
    sessions = [_active_session(position_ms=5000)]
    mock_client = make_mock_plex_client(sessions=sessions, seek_result=True)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 200
    assert resp.json()["seek_to_ms"] == 61000


async def test_skip_session_with_profanity_enabled_only_ignores_nudity(http_client):
    await db.insert_segment("g1", "Movie", start_ms=30000, end_ms=60000, confidence=0.9, category="sex_nudity_immodesty")
    await db.insert_segment("g1", "Movie", start_ms=10000, end_ms=20000, confidence=0.9, category="language_profanity")
    await db.set_user_preference("alice", "sex_nudity_immodesty", enabled=False, threshold=0.5)
    await db.set_user_preference("alice", "language_profanity", enabled=True, threshold=0.5)
    sessions = [_active_session(position_ms=5000)]
    mock_client = make_mock_plex_client(sessions=sessions, seek_result=True)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 200
    assert resp.json()["seek_to_ms"] == 21000


async def test_skip_session_returns_404_when_all_categories_disabled(http_client):
    await db.insert_segment("g1", "Movie", start_ms=30000, end_ms=60000, confidence=0.9, category="sex_nudity_immodesty")
    await db.insert_segment("g1", "Movie", start_ms=10000, end_ms=20000, confidence=0.9, category="language_profanity")
    await db.set_user_preference("alice", "sex_nudity_immodesty", enabled=False, threshold=0.5)
    await db.set_user_preference("alice", "language_profanity", enabled=False, threshold=0.5)
    sessions = [_active_session(position_ms=5000)]
    mock_client = make_mock_plex_client(sessions=sessions, seek_result=True)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 404


async def test_skip_session_with_missing_preferences_defaults_to_all_disabled(http_client):
    await db.insert_segment("g1", "Movie", start_ms=30000, end_ms=60000, confidence=0.9, category="sex_nudity_immodesty")
    await db.insert_segment("g1", "Movie", start_ms=10000, end_ms=20000, confidence=0.9, category="language_profanity")
    sessions = [_active_session(position_ms=5000)]
    mock_client = make_mock_plex_client(sessions=sessions, seek_result=True)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 404


async def test_skip_session_uses_sidecar_when_available(http_client, tmp_path):
    media_path = tmp_path / "Movie.mkv"
    media_path.write_text("stub", encoding="utf-8")
    sidecar_path = media_path.with_suffix(".leapfrog.json")
    sidecar_path.write_text(
        json.dumps(
            {
                "format": "leapfrog.segment.sidecar/v1",
                "media_id": "g1",
                "title": "Movie",
                "segments": [
                    {
                        "start_time": 30.0,
                        "end_time": 60.0,
                        "category": "sex_nudity_immodesty",
                        "source": "subtitles",
                        "confidence": 0.9,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    await db.set_user_preference("alice", "sex_nudity_immodesty", enabled=True, threshold=0.5)
    sessions = [_active_session(position_ms=10000)]
    sessions[0].file_path = str(media_path)
    mock_client = make_mock_plex_client(sessions=sessions, seek_result=True)
    with patch("leapfrog.web.routes.sessions.plex_mod.get_client", return_value=mock_client):
        resp = await http_client.post("/api/sessions/s1/skip")
    assert resp.status_code == 200
    assert resp.json()["seek_to_ms"] == 61000
    assert resp.json()["adapter_status"]["segment_source"] == "sidecar"
