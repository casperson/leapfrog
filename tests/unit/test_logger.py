"""Unit tests for the bounded in-memory log buffer."""

from __future__ import annotations

import json

from leapfrog.logger import (
    clear_log_buffer,
    format_sse_event,
    get_log_entries,
    get_logger,
    setup_logging,
)


def test_log_buffer_is_bounded_and_filterable():
    clear_log_buffer()
    setup_logging("INFO", buffer_capacity=3)
    app_logger = get_logger("leapfrog.test.buffer")
    app_logger.info("first")
    app_logger.warning("second")
    app_logger.error("third")
    app_logger.info("fourth")

    entries = get_log_entries(limit=10)
    assert [entry["message"] for entry in entries] == ["second", "third", "fourth"]

    error_entries = get_log_entries(level="ERROR")
    assert [entry["message"] for entry in error_entries] == ["third"]

    source_entries = get_log_entries(source="leapfrog.test")
    assert len(source_entries) == 3


def test_format_sse_event_wraps_json_payload():
    payload = {"id": 1, "message": "hello", "source": "leapfrog.test"}

    encoded = format_sse_event(payload)

    assert encoded.startswith("event: log\n")
    assert encoded.endswith("\n\n")
    data_line = encoded.splitlines()[1]
    assert data_line.startswith("data: ")
    assert json.loads(data_line.removeprefix("data: ")) == payload
