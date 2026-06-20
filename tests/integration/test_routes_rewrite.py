from __future__ import annotations

import json
from pathlib import Path

import pytest

from leapfrog import database as db
import leapfrog.bg_jobs as bg_jobs
import leapfrog.media_rewriter as media_rewriter


pytestmark = pytest.mark.usefixtures("setup_db")


async def _seed_rewrite_candidate(tmp_path: Path) -> None:
    media_path = tmp_path / "Sample Movie.mp4"
    media_path.write_bytes(b"video")
    await db.upsert_scan_job(
        plex_guid="movie-1",
        title="Sample Movie",
        file_path=str(media_path),
        rating_key="rk-1",
        library_id="1",
        library_title="Movies",
        media_type="movie",
        content_rating="R",
        year=2019,
        show_guid="",
    )
    media_path.with_suffix(".leapfrog.json").write_text(
        json.dumps(
            {
                "format": "leapfrog.segment.sidecar/v1",
                "media_id": "movie-1",
                "title": "Sample Movie",
                "segments": [
                    {
                        "media_id": "movie-1",
                        "start_time": 1,
                        "end_time": 3,
                        "category": "language_profanity",
                        "source": "sidecar",
                        "labels": "fuck",
                        "text_excerpt": "f-word",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_rewrite_candidates_endpoint_returns_readiness(http_client, tmp_path):
    await _seed_rewrite_candidate(tmp_path)

    response = await http_client.get("/api/rewrite/candidates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["settings"]["language_mode"] == "mute"
    assert payload["candidates"][0]["rewrite_ready"] is True


@pytest.mark.asyncio
async def test_rewrite_candidates_endpoint_includes_persisted_root_sidecar_only_title(http_client, tmp_path):
    media_path = tmp_path / "Sidecar Only Movie.mp4"
    media_path.write_bytes(b"video")
    media_path.with_suffix(".leapfrog.json").write_text(
        json.dumps(
            {
                "format": "leapfrog.segment.sidecar/v1",
                "media_id": "sidecar-only",
                "title": "Sidecar Only Movie",
                "segments": [
                    {
                        "media_id": "sidecar-only",
                        "start_time": 1,
                        "end_time": 3,
                        "category": "language_profanity",
                        "source": "sidecar",
                        "labels": "fuck",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    await db.set_setting("rewrite_discovery_roots", json.dumps([str(tmp_path)]))
    media_rewriter._invalidate_rewrite_discovery_cache()

    response = await http_client.get("/api/rewrite/candidates")

    assert response.status_code == 200
    candidate = response.json()["candidates"][0]
    assert candidate["file_path"] == str(media_path)
    assert candidate["rewrite_ready"] is True
    assert candidate["media_id"] == f"file:{media_path}"


@pytest.mark.asyncio
async def test_rewrite_plan_endpoint_returns_preview(http_client, tmp_path, monkeypatch):
    await _seed_rewrite_candidate(tmp_path)

    async def fake_probe(file_path: str):
        return {"duration_s": 100.0, "bitrate": 1_500_000, "has_audio": True}

    monkeypatch.setattr(media_rewriter, "_probe_media", fake_probe)

    response = await http_client.post(
        "/api/rewrite/plan",
        json={
            "media_ids": ["movie-1"],
            "selected_leaf_keys": ["fuck"],
            "language_mode": "mute",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["plans"][0]["media_id"] == "movie-1"
    assert payload["plans"][0]["mute_ranges"] == [{"start": 1.0, "end": 3.0}]


@pytest.mark.asyncio
async def test_rewrite_jobs_endpoint_returns_running_status(http_client, monkeypatch):
    async def fake_enqueue(request: dict) -> int:
        return 42

    monkeypatch.setattr(bg_jobs, "enqueue_rewrite_job", fake_enqueue)
    monkeypatch.setattr("leapfrog.web.routes.rewrite_routes.enqueue_rewrite_job", fake_enqueue)

    response = await http_client.post(
        "/api/rewrite/jobs",
        json={
            "media_ids": ["movie-1"],
            "selected_leaf_keys": ["fuck"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "running", "job_id": 42}


@pytest.mark.asyncio
async def test_rewrite_job_status_endpoint_returns_bg_job(http_client):
    job_id = await db.create_bg_job("rewrite")
    await db.update_bg_job(job_id, progress=60)

    response = await http_client.get(f"/api/rewrite/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["job_type"] == "rewrite"
    assert response.json()["progress"] == 60
