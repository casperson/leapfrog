"""Jellyfin adapter for canonical segment exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SegmentExportAdapter

if TYPE_CHECKING:
    from ..segment_export import CanonicalMediaExport


class JellyfinSegmentAdapter(SegmentExportAdapter):
    """Adapt canonical exports to a Jellyfin-oriented lookup payload."""

    name = "jellyfin"

    def adapt(self, media_export: "CanonicalMediaExport") -> dict[str, Any]:
        return {
            "adapter": self.name,
            "lookup": {
                "item_id": media_export.external_ids.get("plex_guid", media_export.media_id),
                "title": media_export.title,
            },
            "media": media_export.to_record(),
        }
