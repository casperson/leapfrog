"""Plex adapter for canonical segment exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SegmentExportAdapter

if TYPE_CHECKING:
    from ..segment_export import CanonicalMediaExport


class PlexSegmentAdapter(SegmentExportAdapter):
    """Adapt canonical exports to a Plex-oriented lookup payload."""

    name = "plex"

    def adapt(self, media_export: "CanonicalMediaExport") -> dict[str, Any]:
        return {
            "adapter": self.name,
            "lookup": {
                "plex_guid": media_export.external_ids.get("plex_guid", media_export.media_id),
                "plex_rating_key": media_export.external_ids.get("plex_rating_key", ""),
            },
            "media": media_export.to_record(),
        }
