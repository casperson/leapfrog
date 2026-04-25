"""Filter engine: checks playback position against stored segments and seeks past them."""

from __future__ import annotations

import time

from .adapters.plex_runtime import resolve_plex_playback_context
from .logger import get_logger
from .plex_client import ActiveSession, PlexClient

logger = get_logger(__name__)

# Track recently skipped sessions to avoid re-triggering: {session_key: end_ms}
_recently_skipped: dict[str, int] = {}
_seek_backoff_until: dict[str, float] = {}


async def process(
    session: ActiveSession,
    client: PlexClient,
    skip_buffer_ms: int,
    lookahead_ms: int = 5000,
    user_preferences: dict[str, dict[str, float | bool]] | None = None,
) -> None:
    """Check the session's position against stored segments and seek if needed."""
    if not session.is_controllable:
        logger.info("Session %s (%s) is not controllable – skipping", session.session_key, session.full_title)
        return

    pos = session.position_ms

    # Don't re-trigger if we already skipped past this point recently
    skip_until = _recently_skipped.get(session.session_key, 0)
    if pos < skip_until:
        return

    # Back off briefly if a previous seek command failed for this session/client.
    blocked_until = _seek_backoff_until.get(session.session_key, 0.0)
    if time.time() < blocked_until:
        return

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
        return

    if not segments:
        logger.debug(
            "No enabled segments for '%s' after applying preferences for user '%s' (source=%s)",
            session.full_title,
            playback_context.user_id,
            playback_context.segment_source,
        )
        return

    logger.info("Checking %d segment(s) for '%s' at pos=%dms (client=%s)", len(segments), session.full_title, pos, session.client_identifier)
    for seg in segments:
        original_start = int(seg["start_ms"])
        original_end = int(seg["end_ms"])
        trigger_start = max(0, original_start - 5000)
        trigger_end = original_end + 5000
        seek_target = original_end + skip_buffer_ms

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
            )
            if success:
                # Track skip until the expanded segment end to prevent re-triggering
                # if the seek lands slightly before the buffered resume position.
                _recently_skipped[session.session_key] = max(trigger_end, seek_target)
                _seek_backoff_until.pop(session.session_key, None)
            else:
                _seek_backoff_until[session.session_key] = time.time() + 20
            return

    # Clean up stale entries for sessions no longer in range
    if session.session_key in _recently_skipped and pos > _recently_skipped[session.session_key]:
        del _recently_skipped[session.session_key]
    if session.session_key in _seek_backoff_until and time.time() > _seek_backoff_until[session.session_key]:
        del _seek_backoff_until[session.session_key]
