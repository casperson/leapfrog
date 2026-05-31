"""Filter engine: checks playback position against stored segments and seeks past them."""

from __future__ import annotations

import time
from typing import Any

from . import database as db
from .adapters.plex_runtime import resolve_plex_playback_context
from .logger import get_logger
from .plex_client import ActiveSession, PlexClient

logger = get_logger(__name__)

# Track recently skipped sessions to avoid re-triggering the same segment.
_recently_skipped: dict[str, dict[str, int | str]] = {}
_seek_backoff_until: dict[str, float] = {}


def get_recent_skip_until(session_key: str) -> int:
    """Return the current debounce end position for a session."""
    recent = _recently_skipped.get(session_key)
    return int(recent.get("until_ms", 0)) if recent else 0


def _segment_skip_key(session: ActiveSession, segment: dict[str, Any]) -> str:
    media_id = str(segment.get("media_id") or session.plex_guid or session.rating_key)
    return "|".join(
        [
            media_id,
            str(segment.get("start_ms") or ""),
            str(segment.get("end_ms") or ""),
            str(segment.get("category") or "nudity"),
            str(segment.get("source") or ""),
            str(segment.get("labels") or ""),
        ]
    )


async def _record_skip_event(
    session: ActiveSession,
    segment: dict[str, Any],
    *,
    seek_to_ms: int,
    success: bool,
    detail: str,
) -> None:
    try:
        await db.insert_skip_event(
            session_key=session.session_key,
            user_id=session.user,
            media_id=str(segment.get("media_id") or session.plex_guid),
            title=session.full_title,
            position_ms=session.position_ms,
            seek_to_ms=seek_to_ms,
            segment_start_ms=int(segment["start_ms"]),
            segment_end_ms=int(segment["end_ms"]),
            category=str(segment.get("category") or "nudity"),
            source=str(segment.get("source") or ""),
            labels=str(segment.get("labels") or ""),
            client_identifier=session.client_identifier,
            client_title=session.client_title,
            client_address=session.client_address,
            client_port=session.client_port,
            success=success,
            detail=detail,
        )
    except Exception as exc:
        logger.warning("Could not persist skip event for session %s: %s", session.session_key, exc)


async def process(
    session: ActiveSession,
    client: PlexClient,
    skip_buffer_ms: int,
    lookahead_ms: int = 5000,
    user_preferences: dict[str, dict[str, float | bool]] | None = None,
) -> dict[str, Any] | None:
    """Check the session's position against stored segments and seek if needed."""
    if not session.is_controllable:
        logger.info("Session %s (%s) is not controllable – skipping", session.session_key, session.full_title)
        return

    pos = session.position_ms

    # Don't re-trigger if we already skipped past this point recently
    skip_until = get_recent_skip_until(session.session_key)
    if pos < skip_until:
        return None

    # Back off briefly if a previous seek command failed for this session/client.
    blocked_until = _seek_backoff_until.get(session.session_key, 0.0)
    if time.time() < blocked_until:
        return None

    playback_context = await resolve_plex_playback_context(
        session,
        user_preferences=user_preferences,
    )
    segments = [dict(segment) for segment in playback_context.effective_segments]

    if not playback_context.all_segments:
        logger.info(
            "No segments found for '%s' (guid=%s, rating_key=%s, source=%s)",
            session.full_title,
            session.plex_guid,
            session.rating_key,
            playback_context.segment_source,
        )
        return None

    if not segments:
        logger.debug(
            "No enabled segments for '%s' after applying preferences for user '%s' (source=%s)",
            session.full_title,
            playback_context.user_id,
            playback_context.segment_source,
        )
        return None

    logger.info("Checking %d segment(s) for '%s' at pos=%dms (client=%s)", len(segments), session.full_title, pos, session.client_identifier)
    for seg in segments:
        original_start = int(seg["start_ms"])
        original_end = int(seg["end_ms"])
        trigger_start = max(0, original_start - 5000)
        trigger_end = original_end + 5000
        seek_target = original_end + skip_buffer_ms
        skip_key = _segment_skip_key(session, seg)

        # Once playback has already advanced beyond the buffered resume point,
        # this segment is stale for seek purposes and must not trigger a
        # backward jump.
        if pos >= seek_target:
            continue

        # Trigger when approaching the segment (within lookahead_ms before start) or already inside.
        # This compensates for polling latency so the seek fires before/at the segment start.
        if trigger_start - lookahead_ms <= pos <= trigger_end:
            logger.info(
                "Skipping [%s] for user '%s': %dms → %dms (category=%s, source=%s, segment=%d–%d, confidence=%s)",
                session.full_title,
                session.user,
                pos,
                seek_target,
                seg.get("category", "nudity"),
                seg.get("source", "nudenet"),
                trigger_start,
                trigger_end,
                f"{float(seg['confidence']):.2f}" if seg.get("confidence") is not None else "n/a",
            )
            success = await client.seek(
                session.client_identifier,
                seek_target,
                session.client_address,
                session.client_port,
                session.client_title,
            )
            await _record_skip_event(
                session,
                seg,
                seek_to_ms=seek_target,
                success=success,
                detail="seek accepted" if success else "seek failed",
            )
            result = {
                "time": "",
                "user": session.user,
                "title": session.full_title,
                "position_ms": session.position_ms,
                "client": session.client_title,
                "media_id": seg.get("media_id") or session.plex_guid,
                "seek_to_ms": seek_target,
                "segment_start_ms": original_start,
                "segment_end_ms": original_end,
                "category": seg.get("category", "nudity"),
                "source": seg.get("source", ""),
                "labels": seg.get("labels", ""),
                "client_identifier": session.client_identifier,
                "success": success,
            }
            if success:
                # Track skip until the expanded segment end to prevent re-triggering
                # if the seek lands slightly before the buffered resume position.
                _recently_skipped[session.session_key] = {
                    "key": skip_key,
                    "until_ms": max(trigger_end, seek_target),
                }
                _seek_backoff_until.pop(session.session_key, None)
            else:
                _seek_backoff_until[session.session_key] = time.time() + 20
            return result

    # Clean up stale entries for sessions no longer in range
    if session.session_key in _recently_skipped and pos > get_recent_skip_until(session.session_key):
        del _recently_skipped[session.session_key]
    if session.session_key in _seek_backoff_until and time.time() > _seek_backoff_until[session.session_key]:
        del _seek_backoff_until[session.session_key]
    return None
