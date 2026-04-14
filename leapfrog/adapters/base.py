"""Adapter interfaces for canonical exports and runtime playback resolution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime_common import RuntimeMediaReference, RuntimePlaybackContext
    from ..segment_export import CanonicalMediaExport


class SegmentExportAdapter(ABC):
    """Translate canonical media exports into target-specific payloads."""

    name: str

    @abstractmethod
    def adapt(self, media_export: "CanonicalMediaExport") -> dict[str, Any]:
        """Return a target-specific payload for the canonical export."""


class RuntimePlaybackAdapter(ABC):
    """Resolve effective playback segments for a media-server-specific reference."""

    name: str

    @abstractmethod
    async def resolve_playback_context(
        self,
        reference: "RuntimeMediaReference",
        *,
        user_preferences: dict[str, dict[str, float | bool | None]] | None = None,
    ) -> "RuntimePlaybackContext":
        """Return the runtime playback context for this adapter."""

    async def resolve_media_id(self, reference: "RuntimeMediaReference") -> str | None:
        """Resolve the canonical media identifier for a runtime reference."""
        context = await self.resolve_playback_context(reference)
        return context.media_id if context.media_resolved else None

    async def get_effective_segments(
        self,
        reference: "RuntimeMediaReference",
        *,
        user_preferences: dict[str, dict[str, float | bool | None]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return effective skip segments for a runtime reference."""
        context = await self.resolve_playback_context(
            reference,
            user_preferences=user_preferences,
        )
        return context.effective_segments

    async def get_scan_status(self, reference: "RuntimeMediaReference") -> dict[str, Any]:
        """Return adapter status for a runtime reference."""
        context = await self.resolve_playback_context(reference)
        return context.to_status_record()
