"""Resolve playback context for the active server session."""

from __future__ import annotations

from .adapters.emby_runtime import EmbyPlaybackReference, resolve_emby_playback_context
from .adapters.jellyfin_runtime import JellyfinPlaybackReference, resolve_jellyfin_playback_context
from .adapters.plex_runtime import resolve_plex_playback_context
from .media_server import ServerSession
from .plex_client import ActiveSession


def _as_plex_session(session: ServerSession) -> ActiveSession:
    return ActiveSession(
        session_key=session.session_key,
        user=session.user,
        title=session.title,
        full_title=session.full_title,
        plex_guid=session.media_id,
        rating_key=session.rating_key,
        media_type=session.media_type,
        position_ms=session.position_ms,
        duration_ms=session.duration_ms,
        client_identifier=session.client_identifier,
        client_title=session.client_title,
        is_controllable=session.is_controllable,
        thumb=session.thumb,
        client_address=session.client_address,
        client_port=session.client_port,
        library_section_id=session.library_section_id,
        file_path=session.file_path,
    )


async def resolve_playback_context_for_session(session: ServerSession, *, user_preferences=None):
    """Resolve playback context for a canonical active session."""
    if session.adapter == "plex":
        return await resolve_plex_playback_context(
            _as_plex_session(session),
            user_preferences=user_preferences,
        )
    if session.adapter == "jellyfin":
        return await resolve_jellyfin_playback_context(
            JellyfinPlaybackReference(
                user_name=session.user,
                item_id=(session.external_ids or {}).get("jellyfin_item_id", session.media_id),
                title=session.full_title or session.title,
                file_path=session.file_path,
                canonical_media_id=session.media_id,
            ),
            user_preferences=user_preferences,
        )
    if session.adapter == "emby":
        return await resolve_emby_playback_context(
            EmbyPlaybackReference(
                user_name=session.user,
                item_id=(session.external_ids or {}).get("emby_item_id", session.media_id),
                title=session.full_title or session.title,
                file_path=session.file_path,
                canonical_media_id=session.media_id,
            ),
            user_preferences=user_preferences,
        )
    raise ValueError(f"Unsupported runtime adapter: {session.adapter}")
