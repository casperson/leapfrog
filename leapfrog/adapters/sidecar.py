"""Sidecar JSON adapter for canonical segment exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SegmentExportAdapter

if TYPE_CHECKING:
    from ..segment_export import CanonicalMediaExport


class SidecarSegmentAdapter(SegmentExportAdapter):
    """Adapt canonical media exports to the portable sidecar JSON shape."""

    name = "sidecar"

    def adapt(self, media_export: "CanonicalMediaExport") -> dict[str, Any]:
        return {
            "format": "leapfrog.segment.sidecar/v1",
            "media_id": media_export.media_id,
            "title": media_export.title,
            "external_ids": media_export.external_ids,
            "segments": [
                {
                    "media_id": segment.media_id,
                    "start_time": segment.start_time,
                    "end_time": segment.end_time,
                    "category": segment.category,
                    "source": segment.source,
                    "confidence": segment.confidence,
                    "text_excerpt": segment.text_excerpt,
                    "created_at": segment.created_at,
                    "updated_at": segment.updated_at,
                }
                for segment in media_export.segments
            ],
        }
