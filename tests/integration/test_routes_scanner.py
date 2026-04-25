"""Integration tests for scanner queue and mutation API routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from leapfrog import database as db


pytestmark = pytest.mark.usefixtures("setup_db")


async def _make_job(guid: str, *, title: str = "Movie", rating_key: str = "100", library_id: str = "lib1"):
    await db.upsert_scan_job(
        plex_guid=guid,
        title=title,
        file_path=f"/media/{guid}.mkv",
        rating_key=rating_key,
        library_id=library_id,
        library_title="Movies",
    )


async def test_get_scan_queue_returns_snapshot(http_client):
    await _make_job("job-guid")
    await db.queue_scan_job("job-guid")

    resp = await http_client.get("/api/scan/queue")

    assert resp.status_code == 200
    data = resp.json()
    assert data["queue_size"] == 1
    assert data["jobs"][0]["plex_guid"] == "job-guid"


async def test_scan_title_queues_existing_job(http_client):
    await _make_job("scan-me")

    resp = await http_client.post("/api/scan/title", json={"plex_guid": "scan-me"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["queued"] == "scan-me"
    queued = await db.get_queue_snapshot()
    assert [job["plex_guid"] for job in queued] == ["scan-me"]


async def test_scan_title_force_moves_job_to_front(http_client):
    await _make_job("first", rating_key="1")
    await _make_job("force-me", rating_key="2")
    await db.queue_scan_job("first")

    resp = await http_client.post("/api/scan/title", json={"plex_guid": "force-me", "now": True})

    assert resp.status_code == 200
    queued = await db.get_queue_snapshot()
    assert [job["plex_guid"] for job in queued] == ["force-me", "first"]


async def test_scan_title_returns_404_when_job_not_found(http_client):
    with patch("leapfrog.web.routes.scanner_routes.plex_mod.get_client", side_effect=RuntimeError):
        resp = await http_client.post("/api/scan/title", json={"plex_guid": "no-such-guid"})
    assert resp.status_code == 404


async def test_pause_and_resume_scanner(http_client):
    with patch("leapfrog.web.routes.scanner_routes.scan_mod.pause_scanner") as mock_pause:
        pause_resp = await http_client.post("/api/scan/pause")
    with patch("leapfrog.web.routes.scanner_routes.scan_mod.resume_scanner") as mock_resume:
        resume_resp = await http_client.post("/api/scan/resume")

    assert pause_resp.status_code == 200
    assert pause_resp.json()["paused"] is True
    mock_pause.assert_called_once()
    assert resume_resp.status_code == 200
    assert resume_resp.json()["paused"] is False
    mock_resume.assert_called_once()


async def test_skip_current_scan_returns_404_when_nothing_scanning(http_client):
    with patch("leapfrog.web.routes.scanner_routes.scan_mod.get_current_scan", return_value=None):
        resp = await http_client.post("/api/scan/skip-current", json={})
    assert resp.status_code == 404


async def test_skip_current_scan_marks_active_job(http_client):
    await _make_job("active-guid")
    with patch("leapfrog.web.routes.scanner_routes.scan_mod.get_current_scan", return_value="active-guid"), \
         patch("leapfrog.web.routes.scanner_routes.scan_mod.skip_current_scan") as mock_skip:
        resp = await http_client.post("/api/scan/skip-current", json={})

    assert resp.status_code == 200
    assert resp.json()["skipped"] == "active-guid"
    mock_skip.assert_called_once()
    job = await db.get_scan_job_by_guid("active-guid")
    assert job["cancel_requested"] == 1


async def test_cancel_active_scan_by_guid(http_client):
    await _make_job("active-guid")
    with patch("leapfrog.web.routes.scanner_routes.scan_mod.request_skip_scan", return_value=True) as mock_skip:
        resp = await http_client.post("/api/scan/active/active-guid/cancel")

    assert resp.status_code == 200
    assert resp.json()["canceled"] == "active-guid"
    mock_skip.assert_called_once_with("active-guid")


async def test_reorder_queue_snapshot(http_client):
    await _make_job("a")
    await _make_job("b")
    await _make_job("c")
    await db.queue_scan_job("a")
    await db.queue_scan_job("b")
    await db.queue_scan_job("c")

    resp = await http_client.post("/api/scan/queue/reorder", json={"ordered_guids": ["c", "a", "b"]})

    assert resp.status_code == 200
    queued = await db.get_queue_snapshot()
    assert [job["plex_guid"] for job in queued] == ["c", "a", "b"]


async def test_move_queue_item_up_and_down(http_client):
    await _make_job("a")
    await _make_job("b")
    await _make_job("c")
    await db.queue_scan_job("a")
    await db.queue_scan_job("b")
    await db.queue_scan_job("c")

    up_resp = await http_client.post("/api/scan/queue/c/move-up")
    down_resp = await http_client.post("/api/scan/queue/a/move-down")

    assert up_resp.status_code == 200
    assert down_resp.status_code == 200
    queued = await db.get_queue_snapshot()
    assert [job["plex_guid"] for job in queued] == ["c", "a", "b"]


async def test_move_queue_item_top_and_bottom(http_client):
    await _make_job("a")
    await _make_job("b")
    await _make_job("c")
    await db.queue_scan_job("a")
    await db.queue_scan_job("b")
    await db.queue_scan_job("c")

    await http_client.post("/api/scan/queue/b/move-top")
    await http_client.post("/api/scan/queue/a/move-bottom")

    queued = await db.get_queue_snapshot()
    assert [job["plex_guid"] for job in queued] == ["b", "c", "a"]


async def test_cancel_queue_item(http_client):
    await _make_job("a")
    await db.queue_scan_job("a")

    resp = await http_client.post("/api/scan/queue/a/cancel")

    assert resp.status_code == 200
    queued = await db.get_queue_snapshot()
    assert queued == []


async def test_cancel_selected_queue_items(http_client):
    await _make_job("a")
    await _make_job("b")
    await _make_job("c")
    await db.queue_scan_job("a")
    await db.queue_scan_job("b")
    await db.queue_scan_job("c")

    resp = await http_client.post("/api/scan/queue/cancel-selected", json={"guids": ["a", "c"]})

    assert resp.status_code == 200
    assert resp.json()["canceled"] == 2
    queued = await db.get_queue_snapshot()
    assert [job["plex_guid"] for job in queued] == ["b"]


async def test_cancel_all_queue_items(http_client):
    await _make_job("a")
    await _make_job("b")
    await db.queue_scan_job("a")
    await db.queue_scan_job("b")

    resp = await http_client.post("/api/scan/queue/cancel-all")

    assert resp.status_code == 200
    assert resp.json()["canceled"] == 2
    queued = await db.get_queue_snapshot()
    assert queued == []


async def test_queue_unscanned_movies_only_queues_pending_movies(http_client):
    await _make_job("movie-r", title="R Movie", rating_key="1")
    await _make_job("movie-pg", title="PG Movie", rating_key="2")
    await _make_job("episode", title="Episode", rating_key="3")
    async with db.get_connection() as conn:
        await conn.execute("UPDATE scan_jobs SET media_type='movie', content_rating='R' WHERE plex_guid='movie-r'")
        await conn.execute("UPDATE scan_jobs SET media_type='movie', content_rating='PG' WHERE plex_guid='movie-pg'")
        await conn.execute("UPDATE scan_jobs SET media_type='episode' WHERE plex_guid='episode'")
        await conn.commit()

    resp = await http_client.post("/api/scan/queue-unscanned", json={"media_type": "movie"})

    assert resp.status_code == 200
    assert resp.json()["queued"] == 2
    queued = await db.get_queue_snapshot()
    assert [job["plex_guid"] for job in queued] == ["movie-r", "movie-pg"]


async def test_queue_unscanned_now_marks_force_scan(http_client):
    await _make_job("movie-now", title="Movie Now", rating_key="1")

    resp = await http_client.post("/api/scan/queue-unscanned", json={"media_type": "movie", "now": True})

    assert resp.status_code == 200
    queued = await db.get_queue_snapshot()
    assert [job["plex_guid"] for job in queued] == ["movie-now"]
    assert queued[0]["force_scan"] == 1


async def test_toggle_ignored_sets_flag(http_client):
    await _make_job("ig-guid")

    resp = await http_client.post("/api/scan/title/ig-guid/ignore", json={"ignored": True})

    assert resp.status_code == 200
    assert resp.json()["ignored"] is True
    job = await db.get_scan_job_by_guid("ig-guid")
    assert job["ignored"] == 1


async def test_restart_scanner_endpoint_requests_restart(http_client):
    with patch("leapfrog.web.routes.scanner_routes.scan_mod.request_scanner_restart", new=AsyncMock()) as restart:
        resp = await http_client.post("/api/scan/restart-scanner")
    assert resp.status_code == 200
    restart.assert_awaited_once()
