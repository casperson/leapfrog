"""Application logging setup plus bounded in-memory log replay support."""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any


def _normalize_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    if not level:
        return logging.NOTSET
    return int(getattr(logging, str(level).upper(), logging.NOTSET))


class LogBuffer:
    """Thread-safe bounded in-memory log buffer for UI replay and SSE."""

    def __init__(self, capacity: int = 1000) -> None:
        self._capacity = max(1, int(capacity))
        self._entries: deque[dict[str, Any]] = deque(maxlen=self._capacity)
        self._next_id = 1
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def resize(self, capacity: int) -> None:
        next_capacity = max(1, int(capacity))
        with self._lock:
            current = list(self._entries)[-next_capacity:]
            self._capacity = next_capacity
            self._entries = deque(current, maxlen=next_capacity)

    def append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            record = dict(entry)
            record["id"] = self._next_id
            self._next_id += 1
            self._entries.append(record)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._next_id = 1

    def get_entries(
        self,
        *,
        since_id: int | None = None,
        limit: int | None = None,
        level: str | int | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        threshold = _normalize_level(level)
        source_filter = str(source or "").strip()
        with self._lock:
            rows = list(self._entries)
        filtered = [
            dict(entry)
            for entry in rows
            if (since_id is None or int(entry["id"]) > since_id)
            and (threshold == logging.NOTSET or int(entry["levelno"]) >= threshold)
            and (not source_filter or str(entry["source"]).startswith(source_filter))
        ]
        if limit is not None and limit >= 0:
            filtered = filtered[-int(limit):]
        return filtered


class RingBufferLogHandler(logging.Handler):
    """Log handler that mirrors emitted records into the shared buffer."""

    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
            self._buffer.append(
                {
                    "timestamp": timestamp,
                    "level": record.levelname,
                    "levelno": int(record.levelno),
                    "source": record.name,
                    "module": record.module,
                    "message": record.getMessage(),
                }
            )
        except Exception:
            self.handleError(record)


_log_buffer = LogBuffer()
_ring_handler = RingBufferLogHandler(_log_buffer)
_setup_complete = False


def configure_log_buffer(capacity: int) -> None:
    """Resize the in-memory log buffer while preserving recent entries."""
    _log_buffer.resize(capacity)


def get_log_buffer() -> LogBuffer:
    return _log_buffer


def clear_log_buffer() -> None:
    """Clear buffered logs. Intended for tests."""
    _log_buffer.clear()


def get_log_entries(
    *,
    since_id: int | None = None,
    limit: int | None = None,
    level: str | int | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Return filtered log entries from the bounded in-memory buffer."""
    return _log_buffer.get_entries(
        since_id=since_id,
        limit=limit,
        level=level,
        source=source,
    )


def format_sse_event(
    entry: dict[str, Any],
    *,
    event: str = "log",
) -> str:
    """Return a single SSE event payload for one log entry."""
    return f"event: {event}\ndata: {json.dumps(entry, sort_keys=True)}\n\n"


def setup_logging(level: str = "INFO", buffer_capacity: int = 1000) -> None:
    global _setup_complete

    configure_log_buffer(buffer_capacity)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not _setup_complete:
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        root.handlers.clear()
        root.addHandler(stream_handler)
        root.addHandler(_ring_handler)
        _setup_complete = True

    for handler in root.handlers:
        handler.setLevel(getattr(logging, level.upper(), logging.INFO))

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
