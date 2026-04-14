"""Adapter registries for canonical exports and runtime playback resolution."""

from __future__ import annotations

from .base import RuntimePlaybackAdapter, SegmentExportAdapter
from .emby import EmbySegmentAdapter
from .emby_runtime import EmbyRuntimeAdapter
from .jellyfin import JellyfinSegmentAdapter
from .jellyfin_runtime import JellyfinRuntimeAdapter
from .plex import PlexSegmentAdapter
from .plex_runtime import PlexRuntimeAdapter
from .sidecar import SidecarSegmentAdapter

_ADAPTERS: dict[str, SegmentExportAdapter] = {
    "plex": PlexSegmentAdapter(),
    "emby": EmbySegmentAdapter(),
    "jellyfin": JellyfinSegmentAdapter(),
    "sidecar": SidecarSegmentAdapter(),
}

_RUNTIME_ADAPTERS: dict[str, RuntimePlaybackAdapter] = {
    "plex": PlexRuntimeAdapter(),
    "emby": EmbyRuntimeAdapter(),
    "jellyfin": JellyfinRuntimeAdapter(),
}


def get_segment_adapter(name: str) -> SegmentExportAdapter:
    """Return a registered adapter by name."""
    try:
        return _ADAPTERS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown segment adapter: {name}") from exc


def list_segment_adapters() -> tuple[str, ...]:
    """Return all registered adapter names."""
    return tuple(sorted(_ADAPTERS))


def get_runtime_adapter(name: str) -> RuntimePlaybackAdapter:
    """Return a registered runtime adapter by name."""
    try:
        return _RUNTIME_ADAPTERS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown runtime adapter: {name}") from exc


def list_runtime_adapters() -> tuple[str, ...]:
    """Return all registered runtime adapter names."""
    return tuple(sorted(_RUNTIME_ADAPTERS))
