"""Canonical segment export builders and filtering helpers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import database as db
from .domain import Segment
from .preferences import (
    get_effective_skip_segments,
    get_preference_threshold_settings,
    get_resolved_preferences_for_user,
)


@dataclass(slots=True)
class CanonicalSegmentRecord:
    media_id: str
    start_time: float
    end_time: float
    category: str
    source: str
    confidence: float | None
    text_excerpt: str | None
    labels: str
    created_at: str | None
    updated_at: str | None
    id: int | None = None
    review_status: str | None = None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "CanonicalSegmentRecord":
        normalized = dict(record)
        normalized.update(Segment.from_row(record).to_record(plex_guid=record.get("plex_guid")))
        return cls(
            media_id=str(normalized.get("media_id") or normalized.get("plex_guid") or ""),
            start_time=float(normalized.get("start_time") or 0.0),
            end_time=float(normalized.get("end_time") or 0.0),
            category=str(normalized.get("category") or "nudity"),
            source=str(normalized.get("source") or "nudenet"),
            confidence=(
                float(normalized["confidence"])
                if normalized.get("confidence") is not None
                else None
            ),
            text_excerpt=normalized.get("text_excerpt"),
            labels=str(normalized.get("labels") or ""),
            created_at=normalized.get("created_at"),
            updated_at=normalized.get("updated_at"),
            id=int(normalized["id"]) if normalized.get("id") is not None else None,
            review_status=normalized.get("review_status"),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "media_id": self.media_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "category": self.category,
            "source": self.source,
            "confidence": self.confidence,
            "text_excerpt": self.text_excerpt,
            "labels": self.labels,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "review_status": self.review_status,
        }


@dataclass(slots=True)
class CanonicalMediaExport:
    media_id: str
    title: str
    external_ids: dict[str, str]
    segments: list[CanonicalSegmentRecord]
    scan_statuses: list[dict[str, Any]]

    def to_record(self) -> dict[str, Any]:
        return {
            "format": "leapfrog.segment.export/v1",
            "media_id": self.media_id,
            "title": self.title,
            "external_ids": self.external_ids,
            "segment_count": len(self.segments),
            "scan_statuses": self.scan_statuses,
            "segments": [segment.to_record() for segment in self.segments],
        }


def filter_canonical_segments(
    segments: list[dict[str, Any]] | list[CanonicalSegmentRecord],
    *,
    categories: set[str] | None = None,
    min_confidence: float | None = None,
) -> list[CanonicalSegmentRecord]:
    """Filter canonical segments by category and minimum confidence."""
    selected: list[CanonicalSegmentRecord] = []
    allowed_categories = {value.strip() for value in categories or set() if value.strip()}
    for segment in segments:
        record = (
            segment
            if isinstance(segment, CanonicalSegmentRecord)
            else CanonicalSegmentRecord.from_record(segment)
        )
        if allowed_categories and record.category not in allowed_categories:
            continue
        if min_confidence is not None and record.confidence is not None and record.confidence < min_confidence:
            continue
        selected.append(record)
    return selected


def _build_external_ids(job: dict[str, Any] | None, plex_guid: str) -> dict[str, str]:
    external_ids = {"plex_guid": plex_guid}
    if not job:
        return external_ids
    if job.get("rating_key"):
        external_ids["plex_rating_key"] = str(job["rating_key"])
    if job.get("show_guid"):
        external_ids["plex_show_guid"] = str(job["show_guid"])
    if job.get("library_id"):
        external_ids["library_id"] = str(job["library_id"])
    return external_ids


async def build_canonical_media_export(
    plex_guid: str,
    *,
    user_id: str | None = None,
    categories: set[str] | None = None,
    min_confidence: float | None = None,
) -> CanonicalMediaExport:
    """Return a canonical export payload for one media item."""
    job = await db.get_scan_job_by_guid(plex_guid)
    raw_segments = await db.get_segments_for_guid(plex_guid)
    if user_id:
        threshold_defaults = await get_preference_threshold_settings()
        preferences = await get_resolved_preferences_for_user(
            user_id,
            threshold_defaults=threshold_defaults,
        )
        raw_segments = get_effective_skip_segments(raw_segments, preferences)
    canonical_segments = filter_canonical_segments(
        raw_segments,
        categories=categories,
        min_confidence=min_confidence,
    )
    if not job and not canonical_segments:
        raise LookupError(plex_guid)
    media_id = str((job or {}).get("plex_guid") or plex_guid)
    title = str((job or {}).get("title") or (raw_segments[0].get("title") if raw_segments else "") or "")
    scan_statuses = await db.get_media_scan_statuses_for_media(media_id)
    return CanonicalMediaExport(
        media_id=media_id,
        title=title,
        external_ids=_build_external_ids(job, plex_guid),
        segments=canonical_segments,
        scan_statuses=scan_statuses,
    )


async def build_sidecar_payload(
    plex_guid: str,
    *,
    user_id: str | None = None,
    categories: set[str] | None = None,
    min_confidence: float | None = None,
) -> dict[str, Any]:
    """Return the canonical export adapted to the sidecar format."""
    from .adapters import get_segment_adapter

    media_export = await build_canonical_media_export(
        plex_guid,
        user_id=user_id,
        categories=categories,
        min_confidence=min_confidence,
    )
    return get_segment_adapter("sidecar").adapt(media_export)


async def write_sidecar_file(path: str | Path, payload: dict[str, Any]) -> Path:
    """Persist a sidecar payload to disk as indented JSON."""
    destination = Path(path)
    data = json.dumps(payload, indent=2, sort_keys=True)
    await asyncio.to_thread(destination.write_text, f"{data}\n", "utf-8")
    return destination


async def read_sidecar_file(path: str | Path) -> dict[str, Any]:
    """Load a sidecar payload from disk."""
    source = Path(path)
    raw = await asyncio.to_thread(source.read_text, "utf-8")
    return json.loads(raw)
