"""Live log replay and SSE endpoints backed by the in-memory ring buffer."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ...logger import format_sse_event, get_log_entries

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/history")
async def get_log_history(
    limit: int = Query(default=200, ge=0, le=5000),
    since_id: int | None = Query(default=None, ge=0),
    level: str | None = None,
    source: str | None = None,
):
    """Return a filtered slice of recent log history."""

    entries = get_log_entries(
        since_id=since_id,
        limit=limit,
        level=level,
        source=source,
    )
    return {
        "entries": entries,
        "count": len(entries),
        "next_since_id": entries[-1]["id"] if entries else since_id,
    }


@router.get("/stream")
async def stream_logs(
    replay: int = Query(default=100, ge=0, le=2000),
    level: str | None = None,
    source: str | None = None,
    follow: bool = True,
    poll_ms: int = Query(default=500, ge=100, le=5000),
):
    """Stream filtered log entries as Server-Sent Events."""

    async def event_source():
        last_seen = None
        replay_entries = get_log_entries(limit=replay, level=level, source=source)
        if replay_entries:
            last_seen = int(replay_entries[-1]["id"])
        for entry in replay_entries:
            yield format_sse_event(entry)
        if not follow:
            return

        idle_ticks = 0
        while True:
            fresh = get_log_entries(
                since_id=last_seen,
                limit=None,
                level=level,
                source=source,
            )
            if fresh:
                idle_ticks = 0
                last_seen = int(fresh[-1]["id"])
                for entry in fresh:
                    yield format_sse_event(entry)
            else:
                idle_ticks += 1
                if idle_ticks >= 20:
                    idle_ticks = 0
                    yield ": keep-alive\n\n"
            await asyncio.sleep(poll_ms / 1000)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
