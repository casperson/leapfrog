"""Shared runtime adapter types and segment resolution helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import database as db
from ..domain import DEFAULT_CATEGORY_THRESHOLDS
from ..logger import get_logger
from ..preferences import (
    get_preference_threshold_settings,
    get_resolved_preferences_for_user,
    resolve_user_category_preferences,
)
from ..segment_export import CanonicalSegmentRecord, read_sidecar_file

logger = get_logger(__name__)

_SIDECAR_SUFFIXES = (".leapfrog.json", ".segments.json")
SUPPORTED_SIDECAR_SUFFIXES = _SIDECAR_SUFFIXES


@dataclass(slots=True)
class RuntimeMediaReference:
    adapter: str
    user_name: str
    media_id: str = ""
    title: str = ""
    file_path: str = ""
    external_ids: dict[str, str] = field(default_factory=dict)
    lookup_hints: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RuntimePlaybackContext:
    adapter: str
    connected: bool
    media_resolved: bool
    media_id: str
    title: str
    user_id: str
    preferences_resolved: bool
    segment_source: str
    all_segments: list[dict[str, Any]]
    effective_segments: list[dict[str, Any]]
    external_ids: dict[str, str]
    scan_statuses: list[dict[str, Any]]
    sidecar_path: str | None = None

    @property
    def effective_segment_count(self) -> int:
        return len(self.effective_segments)

    def to_status_record(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "connected": self.connected,
            "media_resolved": self.media_resolved,
            "media_id": self.media_id,
            "title": self.title,
            "user_id": self.user_id,
            "preferences_resolved": self.preferences_resolved,
            "segment_source": self.segment_source,
            "sidecar_path": self.sidecar_path,
            "available_segment_count": len(self.all_segments),
            "effective_segment_count": len(self.effective_segments),
            "external_ids": self.external_ids,
            "scan_statuses": self.scan_statuses,
        }


def trim_user_id(user_name: str) -> str:
    """Normalize a media-server username into the local preference identifier."""
    return (user_name or "").strip()


def candidate_sidecar_paths(file_path: str) -> list[Path]:
    """Return supported sidecar candidates adjacent to a media file."""
    media_path = Path(file_path)
    if not file_path:
        return []
    return [media_path.with_suffix(suffix) for suffix in _SIDECAR_SUFFIXES]


async def find_sidecar_path(file_path: str) -> Path | None:
    """Return the first existing sidecar path next to the media file."""
    for path in candidate_sidecar_paths(file_path):
        if await asyncio.to_thread(path.exists):
            return path
    return None


def normalize_sidecar_segments(
    payload: dict[str, Any],
    *,
    default_media_id: str,
) -> list[CanonicalSegmentRecord]:
    """Convert sidecar JSON segment rows into canonical segment records."""
    media_id = str(payload.get("media_id") or default_media_id)
    segments: list[CanonicalSegmentRecord] = []
    for segment in payload.get("segments", []):
        if not isinstance(segment, dict):
            continue
        segments.append(
            CanonicalSegmentRecord(
                media_id=str(segment.get("media_id") or media_id),
                start_time=float(segment.get("start_time") or 0.0),
                end_time=float(segment.get("end_time") or 0.0),
                category=str(segment.get("category") or "nudity"),
                source=str(segment.get("source") or "sidecar"),
                confidence=(
                    float(segment["confidence"])
                    if segment.get("confidence") is not None
                    else None
                ),
                text_excerpt=segment.get("text_excerpt"),
                created_at=segment.get("created_at"),
                updated_at=segment.get("updated_at"),
                review_status=segment.get("review_status"),
            )
        )
    return segments


def dedupe_segments(
    primary: list[CanonicalSegmentRecord],
    secondary: list[CanonicalSegmentRecord],
) -> list[CanonicalSegmentRecord]:
    """Merge two canonical segment sets without duplicating identical records."""
    merged: list[CanonicalSegmentRecord] = []
    seen: set[tuple[Any, ...]] = set()
    for segment in [*primary, *secondary]:
        key = (
            round(segment.start_time, 3),
            round(segment.end_time, 3),
            segment.category,
            segment.source,
            round(segment.confidence, 3) if segment.confidence is not None else None,
            segment.text_excerpt or "",
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(segment)
    return merged


def segments_to_playback_records(
    segments: list[CanonicalSegmentRecord],
    preferences: dict[str, dict[str, float | bool | None]] | None = None,
) -> list[dict[str, Any]]:
    """Convert canonical segments into playback records with optional preference filtering."""
    records: list[dict[str, Any]] = []
    for segment in segments:
        if preferences is not None:
            pref = preferences.get(segment.category)
            if not pref or not bool(pref.get("enabled")):
                continue
            threshold = pref.get("threshold")
            if threshold is not None and segment.confidence is not None and segment.confidence < float(threshold):
                continue
        records.append(
            {
                "media_id": segment.media_id,
                "start_ms": int(round(segment.start_time * 1000)),
                "end_ms": int(round(segment.end_time * 1000)),
                "start_time": segment.start_time,
                "end_time": segment.end_time,
                "category": segment.category,
                "source": segment.source,
                "confidence": segment.confidence,
                "text_excerpt": segment.text_excerpt,
                "created_at": segment.created_at,
                "updated_at": segment.updated_at,
                "review_status": segment.review_status,
            }
        )
    return records


async def load_db_segments_for_reference(
    reference: RuntimeMediaReference,
    job: dict[str, Any] | None,
) -> list[CanonicalSegmentRecord]:
    """Return canonical segments from the database for a runtime reference."""
    rows: list[dict[str, Any]] = []
    lookup_guid = str((job or {}).get("plex_guid") or reference.media_id or "")
    if lookup_guid:
        rows = await db.get_segments_for_guid(lookup_guid)
    rating_key = reference.lookup_hints.get("rating_key", "")
    if not rows and rating_key:
        rows = await db.get_segments_by_rating_key(rating_key)
    return [CanonicalSegmentRecord.from_record(row) for row in rows]


async def load_sidecar_segments_for_reference(
    reference: RuntimeMediaReference,
    job: dict[str, Any] | None,
    *,
    default_media_id: str,
) -> tuple[list[CanonicalSegmentRecord], str | None]:
    """Return canonical sidecar segments and the path used, if any."""
    file_path = str(reference.file_path or (job or {}).get("file_path") or reference.lookup_hints.get("file_path") or "")
    if not file_path:
        return [], None
    sidecar_path = await find_sidecar_path(file_path)
    if sidecar_path is None:
        return [], None
    try:
        payload = await read_sidecar_file(sidecar_path)
    except Exception as exc:
        logger.warning("Could not read sidecar %s: %s", sidecar_path, exc)
        return [], None
    return normalize_sidecar_segments(payload, default_media_id=default_media_id), str(sidecar_path)


async def resolve_job_for_reference(reference: RuntimeMediaReference) -> dict[str, Any] | None:
    """Resolve the local scan job associated with a runtime reference."""
    if reference.media_id:
        job = await db.get_scan_job_by_guid(reference.media_id)
        if job:
            return job
    plex_guid = reference.lookup_hints.get("plex_guid")
    if plex_guid:
        job = await db.get_scan_job_by_guid(plex_guid)
        if job:
            return job
    rating_key = reference.lookup_hints.get("rating_key")
    if rating_key:
        job = await db.get_scan_job_by_rating_key(rating_key)
        if job:
            return job
    file_path = reference.file_path or reference.lookup_hints.get("file_path", "")
    if file_path:
        job = await db.get_scan_job_by_file_path(file_path)
        if job:
            return job
    return None


async def resolve_runtime_playback_context(
    reference: RuntimeMediaReference,
    *,
    user_preferences: dict[str, dict[str, float | bool | None]] | None = None,
    user_mapper: Callable[[str], str] = trim_user_id,
) -> RuntimePlaybackContext:
    """Resolve a runtime media reference into effective playback segments."""
    job = await resolve_job_for_reference(reference)
    tentative_media_id = str((job or {}).get("plex_guid") or reference.media_id or reference.lookup_hints.get("plex_guid") or "")
    title = str((job or {}).get("title") or reference.title)
    user_id = user_mapper(reference.user_name)

    db_segments = await load_db_segments_for_reference(reference, job)
    sidecar_segments, sidecar_path = await load_sidecar_segments_for_reference(
        reference,
        job,
        default_media_id=tentative_media_id or reference.external_ids.get("media_id", ""),
    )
    if sidecar_segments and db_segments:
        merged_segments = dedupe_segments(sidecar_segments, db_segments)
        segment_source = "sidecar+db"
    elif sidecar_segments:
        merged_segments = sidecar_segments
        segment_source = "sidecar"
    elif db_segments:
        merged_segments = db_segments
        segment_source = "db"
    else:
        merged_segments = []
        segment_source = "none"

    preferences_resolved = True
    if user_preferences is None:
        try:
            threshold_defaults = await get_preference_threshold_settings()
            user_preferences = await get_resolved_preferences_for_user(
                user_id,
                threshold_defaults=threshold_defaults,
            )
        except Exception as exc:
            logger.warning(
                "Could not resolve preferences for %s user '%s': %s",
                reference.adapter,
                user_id,
                exc,
            )
            user_preferences = resolve_user_category_preferences(
                overall_enabled=True,
                stored_preferences={},
                threshold_defaults=DEFAULT_CATEGORY_THRESHOLDS,
            )
            preferences_resolved = False

    resolved_media_id = str(
        (job or {}).get("plex_guid")
        or (merged_segments[0].media_id if merged_segments else tentative_media_id)
        or reference.external_ids.get("media_id", "")
    )
    media_resolved = bool(job or merged_segments)
    scan_statuses = (
        await db.get_media_scan_statuses_for_media(resolved_media_id)
        if resolved_media_id
        else []
    )

    return RuntimePlaybackContext(
        adapter=reference.adapter,
        connected=True,
        media_resolved=media_resolved,
        media_id=resolved_media_id,
        title=title,
        user_id=user_id,
        preferences_resolved=preferences_resolved,
        segment_source=segment_source,
        all_segments=segments_to_playback_records(merged_segments),
        effective_segments=segments_to_playback_records(merged_segments, user_preferences),
        external_ids=dict(reference.external_ids),
        scan_statuses=scan_statuses,
        sidecar_path=sidecar_path,
    )
