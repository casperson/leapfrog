"""Integration tests for log history and SSE streaming routes."""

from __future__ import annotations

import pytest

from leapfrog.logger import clear_log_buffer, get_logger, setup_logging


pytestmark = pytest.mark.usefixtures("setup_db")


async def test_log_history_replays_filtered_entries(http_client):
    clear_log_buffer()
    setup_logging("INFO", buffer_capacity=100)
    app_logger = get_logger("leapfrog.logs.history")
    app_logger.info("history one")
    app_logger.error("history two")

    resp = await http_client.get("/api/logs/history", params={"source": "leapfrog.logs", "level": "ERROR"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["count"] == 1
    assert payload["entries"][0]["message"] == "history two"


async def test_log_stream_formats_sse_payload(http_client):
    clear_log_buffer()
    setup_logging("INFO", buffer_capacity=100)
    app_logger = get_logger("leapfrog.logs.stream")
    app_logger.warning("streamed log entry")

    async with http_client.stream(
        "GET",
        "/api/logs/stream",
        params={"source": "leapfrog.logs.stream", "follow": "false", "replay": 10},
    ) as resp:
        body = await resp.aread()

    text = body.decode("utf-8")
    assert resp.status_code == 200
    assert "event: log" in text
    assert '"message": "streamed log entry"' in text
