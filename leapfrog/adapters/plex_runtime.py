"""Plex runtime adapter for playback-time segment resolution."""

from __future__ import annotations

from dataclasses import dataclass

from .base import RuntimePlaybackAdapter
from .runtime_common import (
    RuntimeMediaReference,
    RuntimePlaybackContext,
    find_sidecar_path,
    resolve_runtime_playback_context,
    trim_user_id,
)
from ..plex_client import ActiveSession

PlexPlaybackContext = RuntimePlaybackContext


def map_plex_user_to_user_id(plex_username: str) -> str:
    """Map a Plex username to the local preference profile identifier."""
    return trim_user_id(plex_username)


class PlexRuntimeAdapter(RuntimePlaybackAdapter):
    """Resolve effective playback segments for Plex sessions."""

    name = "plex"

    async def resolve_playback_context(
        self,
        reference: RuntimeMediaReference,
        *,
        user_preferences: dict[str, dict[str, float | bool | None]] | None = None,
    ) -> RuntimePlaybackContext:
        return await resolve_runtime_playback_context(
            reference,
            user_preferences=user_preferences,
            user_mapper=map_plex_user_to_user_id,
        )


_ADAPTER = PlexRuntimeAdapter()


def build_plex_media_reference(session: ActiveSession) -> RuntimeMediaReference:
    """Return the shared runtime reference for a Plex session."""
    return RuntimeMediaReference(
        adapter="plex",
        user_name=session.user,
        media_id=session.plex_guid,
        title=session.full_title or session.title,
        file_path=session.file_path,
        external_ids={
            "plex_guid": session.plex_guid,
            "plex_rating_key": session.rating_key,
        },
        lookup_hints={
            "plex_guid": session.plex_guid,
            "rating_key": session.rating_key,
            "file_path": session.file_path,
        },
    )


async def resolve_plex_playback_context(
    session: ActiveSession,
    *,
    user_preferences: dict[str, dict[str, float | bool | None]] | None = None,
) -> PlexPlaybackContext:
    """Resolve media, segment source, and effective skip list for a Plex session."""
    return await _ADAPTER.resolve_playback_context(
        build_plex_media_reference(session),
        user_preferences=user_preferences,
    )


__all__ = [
    "PlexPlaybackContext",
    "PlexRuntimeAdapter",
    "build_plex_media_reference",
    "find_sidecar_path",
    "map_plex_user_to_user_id",
    "resolve_plex_playback_context",
]
