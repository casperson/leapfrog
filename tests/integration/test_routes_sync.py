"""Integration tests for /api/sync/* routes."""

from __future__ import annotations

import json

import pytest

from cleanplex import database as db


pytestmark = pytest.mark.usefixtures("setup_db")


# ── GET /api/sync/conflicts ────────────────────────────────────────────────────

async def test_conflicts_returns_400_when_sync_disabled(http_client):
    resp = await http_client.get("/api/sync/conflicts")
    assert resp.status_code == 400


async def _enable_sync(client) -> None:
    await client.post(
        "/api/sync/settings",
        json={"instance_name": "test-instance", "sync_enabled": True},
    )


async def test_conflicts_returns_empty_when_no_multi_source_data(http_client):
    await _enable_sync(http_client)
    resp = await http_client.get("/api/sync/conflicts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["total_conflicts"] == 0
    assert data["conflicts"] == []


async def test_conflicts_returns_empty_when_only_single_source(http_client):
    await _enable_sync(http_client)
    await db.upsert_segment_library_entry(
        file_hash="aabbcc",
        file_name="movie.mkv",
        file_size=1000000,
        duration_ms=7200000,
        segments_json=json.dumps([
            {"start_ms": 10000, "end_ms": 15000, "confidence": 0.9, "labels": "NUDITY"},
        ]),
        source_instance="alpha",
    )
    resp = await http_client.get("/api/sync/conflicts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_conflicts"] == 0


async def test_conflicts_detects_label_disagreement(http_client):
    await _enable_sync(http_client)
    await db.upsert_segment_library_entry(
        file_hash="deadbeef",
        file_name="movie.mkv",
        file_size=1000000,
        duration_ms=7200000,
        segments_json=json.dumps([
            {"start_ms": 10000, "end_ms": 15000, "confidence": 0.9, "labels": "FEMALE_BREAST_EXPOSED"},
        ]),
        source_instance="alpha",
    )
    await db.upsert_segment_library_entry(
        file_hash="deadbeef",
        file_name="movie.mkv",
        file_size=1000000,
        duration_ms=7200000,
        segments_json=json.dumps([
            {"start_ms": 10200, "end_ms": 14900, "confidence": 0.85, "labels": "BUTTOCKS_EXPOSED"},
        ]),
        source_instance="beta",
    )
    resp = await http_client.get("/api/sync/conflicts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_files_with_conflicts"] == 1
    assert data["total_conflicts"] >= 1
    file_conflict = data["conflicts"][0]
    assert file_conflict["file_hash"] == "deadbeef"
    assert set(file_conflict["sources"]) == {"alpha", "beta"}
    types = {c["type"] for c in file_conflict["conflicts"]}
    assert "label_disagreement" in types


async def test_conflicts_detects_unverified_segment(http_client):
    await _enable_sync(http_client)
    await db.upsert_segment_library_entry(
        file_hash="cafebabe",
        file_name="other.mkv",
        file_size=500000,
        duration_ms=3600000,
        segments_json=json.dumps([
            {"start_ms": 10000, "end_ms": 15000, "confidence": 0.9, "labels": "NUDITY"},
            {"start_ms": 60000, "end_ms": 65000, "confidence": 0.8, "labels": "NUDITY"},  # unique
        ]),
        source_instance="alpha",
    )
    await db.upsert_segment_library_entry(
        file_hash="cafebabe",
        file_name="other.mkv",
        file_size=500000,
        duration_ms=3600000,
        segments_json=json.dumps([
            {"start_ms": 10100, "end_ms": 14900, "confidence": 0.85, "labels": "NUDITY"},
        ]),
        source_instance="beta",
    )
    resp = await http_client.get("/api/sync/conflicts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_files_with_conflicts"] == 1
    types = {c["type"] for c in data["conflicts"][0]["conflicts"]}
    assert "unverified" in types


async def test_conflicts_no_conflicts_on_full_agreement(http_client):
    await _enable_sync(http_client)
    await db.upsert_segment_library_entry(
        file_hash="11223344",
        file_name="agreed.mkv",
        file_size=200000,
        duration_ms=5400000,
        segments_json=json.dumps([
            {"start_ms": 10000, "end_ms": 15000, "confidence": 0.9, "labels": "NUDITY"},
        ]),
        source_instance="alpha",
    )
    await db.upsert_segment_library_entry(
        file_hash="11223344",
        file_name="agreed.mkv",
        file_size=200000,
        duration_ms=5400000,
        segments_json=json.dumps([
            {"start_ms": 10100, "end_ms": 14950, "confidence": 0.88, "labels": "NUDITY"},
        ]),
        source_instance="beta",
    )
    resp = await http_client.get("/api/sync/conflicts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_conflicts"] == 0


async def test_conflicts_handles_unparseable_segments_json_gracefully(http_client):
    await _enable_sync(http_client)
    await db.upsert_segment_library_entry(
        file_hash="badjson",
        file_name="corrupt.mkv",
        file_size=100000,
        duration_ms=1000000,
        segments_json="not valid json {{",
        source_instance="broken",
    )
    await db.upsert_segment_library_entry(
        file_hash="badjson",
        file_name="corrupt.mkv",
        file_size=100000,
        duration_ms=1000000,
        segments_json=json.dumps([
            {"start_ms": 5000, "end_ms": 10000, "confidence": 0.9, "labels": "NUDITY"},
        ]),
        source_instance="good",
    )
    # Only one source parses → no multi-source comparison possible → no conflicts
    resp = await http_client.get("/api/sync/conflicts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_conflicts"] == 0
