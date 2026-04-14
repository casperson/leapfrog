"""Jellyfin runtime adapter for playback-time segment resolution."""

from __future__ import annotations

from dataclasses import dataclass

from .base import RuntimePlaybackAdapter
from .runtime_common import RuntimeMediaReference, RuntimePlaybackContext, resolve_runtime_playback_context, trim_user_id

JellyfinPlaybackContext = RuntimePlaybackContext


@dataclass(slots=True)
class JellyfinPlaybackReference:
    user_name: str
    item_id: str
    title: str
    file_path: str = ""
    canonical_media_id: str = ""


def map_jellyfin_user_to_user_id(jellyfin_username: str) -> str:
    """Map a Jellyfin username to the local preference profile identifier."""
    return trim_user_id(jellyfin_username)


class JellyfinRuntimeAdapter(RuntimePlaybackAdapter):
    """Resolve effective playback segments for Jellyfin sessions."""

    name = "jellyfin"

    async def resolve_playback_context(
        self,
        reference: RuntimeMediaReference,
        *,
        user_preferences: dict[str, dict[str, float | bool | None]] | None = None,
    ) -> RuntimePlaybackContext:
        return await resolve_runtime_playback_context(
            reference,
            user_preferences=user_preferences,
            user_mapper=map_jellyfin_user_to_user_id,
        )


_ADAPTER = JellyfinRuntimeAdapter()


def build_jellyfin_media_reference(reference: JellyfinPlaybackReference) -> RuntimeMediaReference:
    """Return the shared runtime reference for a Jellyfin playback item."""
    return RuntimeMediaReference(
        adapter="jellyfin",
        user_name=reference.user_name,
        media_id=reference.canonical_media_id,
        title=reference.title,
        file_path=reference.file_path,
        external_ids={"jellyfin_item_id": reference.item_id},
        lookup_hints={"file_path": reference.file_path},
    )


async def resolve_jellyfin_playback_context(
    reference: JellyfinPlaybackReference,
    *,
    user_preferences: dict[str, dict[str, float | bool | None]] | None = None,
) -> JellyfinPlaybackContext:
    """Resolve media, segment source, and effective skip list for a Jellyfin item."""
    return await _ADAPTER.resolve_playback_context(
        build_jellyfin_media_reference(reference),
        user_preferences=user_preferences,
    )
