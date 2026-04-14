"""Emby adapter for canonical segment exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SegmentExportAdapter

if TYPE_CHECKING:
    from ..segment_export import CanonicalMediaExport


class EmbySegmentAdapter(SegmentExportAdapter):
    """Adapt canonical exports to an Emby-oriented lookup payload."""

    name = "emby"

    def adapt(self, media_export: "CanonicalMediaExport") -> dict[str, Any]:
        return {
            "adapter": self.name,
            "lookup": {
                "provider_id": media_export.external_ids.get("plex_guid", media_export.media_id),
                "title": media_export.title,
            },
            "media": media_export.to_record(),
        }
