"""Emby runtime adapter for playback-time segment resolution."""

from __future__ import annotations

from dataclasses import dataclass

from .base import RuntimePlaybackAdapter
from .runtime_common import RuntimeMediaReference, RuntimePlaybackContext, resolve_runtime_playback_context, trim_user_id

EmbyPlaybackContext = RuntimePlaybackContext


@dataclass(slots=True)
class EmbyPlaybackReference:
    user_name: str
    item_id: str
    title: str
    file_path: str = ""
    canonical_media_id: str = ""


def map_emby_user_to_user_id(emby_username: str) -> str:
    """Map an Emby username to the local preference profile identifier."""
    return trim_user_id(emby_username)


class EmbyRuntimeAdapter(RuntimePlaybackAdapter):
    """Resolve effective playback segments for Emby sessions."""

    name = "emby"

    async def resolve_playback_context(
        self,
        reference: RuntimeMediaReference,
        *,
        user_preferences: dict[str, dict[str, float | bool | None]] | None = None,
    ) -> RuntimePlaybackContext:
        return await resolve_runtime_playback_context(
            reference,
            user_preferences=user_preferences,
            user_mapper=map_emby_user_to_user_id,
        )


_ADAPTER = EmbyRuntimeAdapter()


def build_emby_media_reference(reference: EmbyPlaybackReference) -> RuntimeMediaReference:
    """Return the shared runtime reference for an Emby playback item."""
    return RuntimeMediaReference(
        adapter="emby",
        user_name=reference.user_name,
        media_id=reference.canonical_media_id,
        title=reference.title,
        file_path=reference.file_path,
        external_ids={"emby_item_id": reference.item_id},
        lookup_hints={"file_path": reference.file_path},
    )


async def resolve_emby_playback_context(
    reference: EmbyPlaybackReference,
    *,
    user_preferences: dict[str, dict[str, float | bool | None]] | None = None,
) -> EmbyPlaybackContext:
    """Resolve media, segment source, and effective skip list for an Emby item."""
    return await _ADAPTER.resolve_playback_context(
        build_emby_media_reference(reference),
        user_preferences=user_preferences,
    )
