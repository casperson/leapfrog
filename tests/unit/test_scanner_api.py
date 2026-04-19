"""Unit tests for scanner queue orchestration and active-scan controls."""

from __future__ import annotations

import pytest
import pytest_asyncio

import leapfrog.scanner as scanner
from leapfrog import database as db


pytestmark = pytest.mark.usefixtures("setup_db")


@pytest_asyncio.fixture(autouse=True)
async def reset_scanner_state():
    scanner._current_guids.clear()
    scanner._skip_requested_guids.clear()
    scanner._paused = False
    scanner._queue_size_snapshot = 0
    yield
    scanner._current_guids.clear()
    scanner._skip_requested_guids.clear()
    scanner._paused = False
    scanner._queue_size_snapshot = 0


async def _make_job(guid: str, *, rating_key: str = "1", media_type: str = "movie", show_guid: str = ""):
    await db.upsert_scan_job(
        plex_guid=guid,
        title=guid,
        file_path=f"/tmp/{guid}.mkv",
        rating_key=rating_key,
        library_id="lib",
        library_title="Library",
        media_type=media_type,
        show_guid=show_guid,
    )


def test_get_queue_size_returns_zero_initially():
    assert scanner.get_queue_size() == 0


async def test_enqueue_adds_job_to_persisted_queue():
    await _make_job("guid-1")
    await scanner.enqueue("guid-1")

    queued = await db.get_queue_snapshot()
    assert [job["plex_guid"] for job in queued] == ["guid-1"]
    assert scanner.get_queue_size() == 1


async def test_force_scan_job_moves_title_to_queue_top():
    await _make_job("normal", rating_key="1")
    await _make_job("force", rating_key="2")
    await scanner.enqueue("normal")
    await scanner.force_scan_job("force")

    queued = await db.get_queue_snapshot()
    assert [job["plex_guid"] for job in queued] == ["force", "normal"]
    assert queued[0]["queue_priority"] == 100


async def test_enqueue_pending_orders_movies_before_episodes():
    await _make_job("ep1", rating_key="10", media_type="episode", show_guid="show-a")
    await _make_job("mov1", rating_key="5")
    await _make_job("ep2", rating_key="11", media_type="episode", show_guid="show-a")
    await _make_job("mov2", rating_key="6")

    await scanner.enqueue_pending()

    queued = await db.get_queue_snapshot()
    ordered = [job["plex_guid"] for job in queued]
    assert ordered.index("mov1") < ordered.index("ep1")
    assert ordered.index("mov2") < ordered.index("ep1")


def test_is_paused_initially_false():
    assert scanner.is_paused() is False


def test_pause_and_resume_scanner():
    scanner.pause_scanner()
    assert scanner.is_paused() is True
    scanner.resume_scanner()
    assert scanner.is_paused() is False


def test_get_current_scan_returns_sorted_guid():
    scanner._current_guids.update({"z-guid", "a-guid", "m-guid"})
    assert scanner.get_current_scan() == "a-guid"
    assert scanner.get_current_scans() == ["a-guid", "m-guid", "z-guid"]


def test_request_skip_scan_returns_false_when_not_active():
    assert scanner.request_skip_scan("missing") is False


def test_request_skip_scan_returns_true_when_active():
    scanner._current_guids.add("active")
    assert scanner.request_skip_scan("active") is True
    assert "active" in scanner._skip_requested_guids
