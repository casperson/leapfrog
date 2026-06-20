"""Plan and execute VidAngel-taxonomy media rewrite exports."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from typing import Any

from . import database as db
from .adapters.runtime_common import candidate_sidecar_paths, normalize_sidecar_segments, read_external_segments
from .domain import get_vidangel_leaf_metadata
from .frame_extractor import _FFMPEG_BIN, _FFPROBE_BIN
from .logger import get_logger
from .preferences import (
    build_user_category_preferences_map,
    build_user_filter_map,
    build_user_label_preferences_map,
    get_category_metadata,
    get_default_skip_label_settings,
    get_preference_threshold_settings,
    resolve_preferences_for_users,
)
from .segment_export import read_sidecar_file
from .skip_file_converter import parse_skp_text
from .subtitles import SubtitleCue, parse_subtitle_text

logger = get_logger(__name__)

REWRITE_SIZE_LIMIT_PERCENT = 120
REWRITE_LANGUAGE_MODE_DEFAULT = "mute"
REWRITE_OUTPUT_CONTAINER = "mkv"
REWRITE_OUTPUT_SUFFIX = ".cleaned"
REWRITE_FILE_ID_PREFIX = "file:"
REWRITE_DISCOVERY_ROOTS_KEY = "rewrite_discovery_roots"
REWRITE_SELECTED_LEAF_KEYS = "rewrite_selected_leaf_keys"
REWRITE_LANGUAGE_MODE_KEY = "rewrite_language_mode"
REWRITE_PROFILE_USER_KEY = "rewrite_profile_user"
REWRITE_SIZE_LIMIT_KEY = "rewrite_size_limit_percent"
_ENGLISH_LANGUAGE_TAGS = {"en", "eng", "english"}
# Only include codecs we can safely round-trip through SRT without stripping
# layout/styling semantics from the rewritten subtitle stream.
_TEXT_SUBTITLE_CODECS = {"subrip", "srt", "mov_text", "webvtt", "text"}
_GPU_ENCODER_CANDIDATES = ("h264_videotoolbox", "h264_nvenc", "h264_qsv")
_preferred_video_encoder_cache: str | None | bool = False
_REWRITE_DISCOVERY_CACHE_TTL_SECONDS = 15.0
_rewrite_discovery_cache: tuple[tuple[str, ...], float, list[tuple[Path, Path]]] | None = None


class RewriteEligibilityError(RuntimeError):
    """Raised when a title cannot participate in a rewrite export."""


class RewriteExecutionError(RuntimeError):
    """Raised when ffmpeg rewrite execution fails."""


def _normalized_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _is_language_leaf(leaf_key: str) -> bool:
    metadata = get_vidangel_leaf_metadata(leaf_key) or {}
    return str(metadata.get("category") or "").startswith("language_")


def _adjacent_skp_path(file_path: str) -> Path | None:
    if not file_path:
        return None
    path = Path(file_path)
    candidate = path.with_suffix(".skp")
    return candidate if candidate.exists() else None


def _sidecar_output_path(file_path: str) -> Path:
    return Path(file_path).with_suffix(".leapfrog.json")


def _rewrite_output_path(file_path: str) -> Path:
    source = Path(file_path)
    return source.with_name(f"{source.stem}{REWRITE_OUTPUT_SUFFIX}.{REWRITE_OUTPUT_CONTAINER}")


def _split_labels(raw: str) -> list[str]:
    return [label.strip() for label in str(raw or "").split(",") if label.strip()]


def _stream_language(value: Any) -> str:
    return _normalized_text(value).replace(" ", "")


def _is_english_stream(stream: dict[str, Any]) -> bool:
    language = _stream_language(stream.get("language") or "")
    if language in _ENGLISH_LANGUAGE_TAGS:
        return True
    title = _normalized_text(stream.get("title") or "")
    return "english" in title or title == "eng"


def _rewrite_media_id_for_file(file_path: str) -> str:
    return f"{REWRITE_FILE_ID_PREFIX}{file_path}"


def _file_path_from_rewrite_media_id(media_id: str) -> str | None:
    value = str(media_id or "")
    if not value.startswith(REWRITE_FILE_ID_PREFIX):
        return None
    return value[len(REWRITE_FILE_ID_PREFIX):].strip() or None


def _source_base_name(path: Path) -> str:
    name = path.name
    for suffix in (".leapfrog.json", ".segments.json", ".edl", ".csv", ".skp"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _infer_media_path_from_source(source_path: Path) -> Path | None:
    directory = source_path.parent
    base_name = _source_base_name(source_path)
    for candidate in directory.iterdir():
        if not candidate.is_file():
            continue
        if candidate == source_path:
            continue
        if candidate.name == base_name:
            return candidate
        if candidate.stem == base_name and candidate.suffix.lower() not in {".json", ".csv", ".edl", ".skp"}:
            return candidate
    return None


def _known_media_roots(jobs: list[dict[str, Any]]) -> list[Path]:
    roots = [
        Path(str(job.get("file_path") or "")).parent
        for job in jobs
        if str(job.get("file_path") or "").strip()
    ]
    return _minimal_root_set(roots)


def _minimal_root_set(roots: list[Path]) -> list[Path]:
    normalized_roots = sorted({str(path) for path in roots if str(path).strip()})
    selected: list[Path] = []
    for raw_root in normalized_roots:
        root = Path(raw_root)
        if any(str(root).startswith(f"{existing}{os.sep}") or str(root) == existing for existing in map(str, selected)):
            continue
        selected = [existing for existing in selected if not str(existing).startswith(f"{root}{os.sep}")]
        selected.append(root)
    return selected


def _discover_rewrite_sources_in_roots(roots: list[Path]) -> list[tuple[Path, Path]]:
    discovered: list[tuple[Path, Path]] = []
    seen_media_paths: set[str] = set()
    supported_suffixes = (".leapfrog.json", ".segments.json", ".edl", ".csv", ".skp")
    for root in roots:
        if not root.exists():
            continue
        for directory, _, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith(supported_suffixes):
                    continue
                source_path = Path(directory) / filename
                media_path = _infer_media_path_from_source(source_path)
                if media_path is None:
                    continue
                media_key = str(media_path)
                if media_key in seen_media_paths:
                    continue
                seen_media_paths.add(media_key)
                discovered.append((media_path, source_path))
    return discovered


def _invalidate_rewrite_discovery_cache() -> None:
    global _rewrite_discovery_cache
    _rewrite_discovery_cache = None


async def _get_persisted_rewrite_discovery_roots() -> list[Path]:
    raw_roots = await db.get_setting(REWRITE_DISCOVERY_ROOTS_KEY, "[]")
    try:
        parsed = json.loads(raw_roots)
    except Exception:
        return []
    roots = [Path(str(value).strip()) for value in parsed if str(value).strip()]
    return _minimal_root_set(roots)


async def remember_rewrite_discovery_roots(file_paths: list[str]) -> None:
    """Persist minimal media roots discovered from Plex library enumeration."""
    candidate_roots = _minimal_root_set(
        [Path(str(file_path).strip()).parent for file_path in file_paths if str(file_path).strip()]
    )
    if not candidate_roots:
        return
    existing_roots = await _get_persisted_rewrite_discovery_roots()
    combined_roots = _minimal_root_set([*existing_roots, *candidate_roots])
    existing_serialized = [str(path) for path in existing_roots]
    combined_serialized = [str(path) for path in combined_roots]
    if combined_serialized != existing_serialized:
        await db.set_setting(REWRITE_DISCOVERY_ROOTS_KEY, json.dumps(combined_serialized))
    _invalidate_rewrite_discovery_cache()


async def _discover_rewrite_sources_with_cache(roots: list[Path]) -> list[tuple[Path, Path]]:
    global _rewrite_discovery_cache
    root_key = tuple(str(path) for path in roots)
    loop = asyncio.get_running_loop()
    now = loop.time()
    if _rewrite_discovery_cache is not None:
        cached_root_key, cached_until, cached_sources = _rewrite_discovery_cache
        if cached_root_key == root_key and cached_until > now:
            return list(cached_sources)
    discovered = await asyncio.to_thread(_discover_rewrite_sources_in_roots, roots)
    # Cache the directory walk briefly so the rewrite page can poll without
    # re-traversing large media trees on every request.
    _rewrite_discovery_cache = (root_key, now + _REWRITE_DISCOVERY_CACHE_TTL_SECONDS, list(discovered))
    return discovered


async def _resolve_media_record(media_id: str) -> tuple[str, str, dict[str, Any] | None]:
    file_media_path = _file_path_from_rewrite_media_id(media_id)
    if file_media_path is not None:
        job = await db.get_scan_job_by_file_path(file_media_path)
        title = str((job or {}).get("title") or Path(file_media_path).stem)
        return file_media_path, title, job
    jobs_by_guid = await db.get_scan_jobs_by_guids([media_id])
    job = jobs_by_guid.get(media_id)
    if not job or not str(job.get("file_path") or "").strip():
        raise RewriteEligibilityError("Media file not found for rewrite.")
    return str(job["file_path"]), str(job.get("title") or media_id), job


async def _build_candidate_record(
    *,
    file_path: str,
    source_path: Path | None = None,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    media_id = str((job or {}).get("plex_guid") or _rewrite_media_id_for_file(file_path))
    title = str((job or {}).get("title") or Path(file_path).stem)
    readiness = False
    reason = ""
    source_type = ""
    sidecar_path = ""
    leaf_count = 0
    try:
        if source_path is None:
            source_path = await _find_adjacent_rewrite_source(file_path)
        if source_path is None:
            raise RewriteEligibilityError("No adjacent sidecar or SKP file was found.")
        payload, normalized_source_path, source_type = await normalize_rewrite_input(
            file_path=file_path,
            media_id=media_id,
            title=title,
        )
        sidecar_path = normalized_source_path
        normalized_segments = normalize_sidecar_segments(payload, default_media_id=media_id)
        leaf_count = len(
            {
                label
                for segment in normalized_segments
                for label in _split_labels(segment.labels)
                if get_vidangel_leaf_metadata(label)
            }
        )
        readiness = True
    except RewriteEligibilityError as exc:
        reason = str(exc)
        if source_path is not None:
            source_type = "skp-normalized" if source_path.suffix.lower() == ".skp" else "sidecar"
            sidecar_path = str(source_path)
    return {
        "media_id": media_id,
        "title": title,
        "file_path": file_path,
        "library_id": str((job or {}).get("library_id") or ""),
        "library_title": str((job or {}).get("library_title") or ""),
        "media_type": str((job or {}).get("media_type") or "movie"),
        "year": (job or {}).get("year"),
        "sidecar_path": sidecar_path,
        "source_type": source_type,
        "rewrite_ready": readiness,
        "unsupported_reason": reason or None,
        "available_leaf_count": leaf_count,
        "output_path": str(_rewrite_output_path(file_path)),
    }


def _find_leaf_key_from_text(text: str) -> str | None:
    target = _normalized_text(text)
    if not target:
        return None
    direct = _normalized_text(text).replace(" ", "_")
    if get_vidangel_leaf_metadata(direct):
        return direct
    matches: list[str] = []
    for category in get_vidangel_category_metadata():
        for label in category.get("labels", []):
            leaf_key = str(label.get("key") or "").strip()
            if not leaf_key:
                continue
            if _normalized_text(leaf_key) == target or _normalized_text(label.get("label") or "") == target:
                matches.append(leaf_key)
    if len(matches) == 1:
        return matches[0]
    return None


def get_vidangel_category_metadata() -> list[dict[str, Any]]:
    """Return category metadata synchronously from the domain snapshot."""
    from .domain import CATEGORY_LABEL_DEFINITIONS, CATEGORY_DEFINITIONS, DEFAULT_SKIP_LABELS

    return [
        {
            "key": definition.key,
            "label": definition.label,
            "description": definition.description,
            "labels": [
                {
                    "key": label.key,
                    "label": label.label,
                    "description": label.description,
                    "default_skip": label.key in DEFAULT_SKIP_LABELS.get(definition.key, []),
                }
                for label in CATEGORY_LABEL_DEFINITIONS.get(definition.key, ())
            ],
        }
        for definition in CATEGORY_DEFINITIONS
    ]


def _normalize_segment_labels(segment: dict[str, Any]) -> list[str]:
    labels = _split_labels(segment.get("labels") or segment.get("tags") or "")
    if labels:
        if all(get_vidangel_leaf_metadata(label) for label in labels):
            return labels
        raise RewriteEligibilityError("Segment labels are not exact VidAngel leaf keys.")
    inferred = _find_leaf_key_from_text(str(segment.get("text_excerpt") or ""))
    if inferred:
        return [inferred]
    raise RewriteEligibilityError("Segment cannot be mapped to an exact VidAngel leaf key.")


async def _normalize_payload_from_sidecar(path: Path, *, media_id: str) -> dict[str, Any]:
    payload = await read_sidecar_file(path)
    normalized_segments: list[dict[str, Any]] = []
    for segment in payload.get("segments", []):
        if not isinstance(segment, dict):
            continue
        labels = _normalize_segment_labels(segment)
        row = dict(segment)
        row["media_id"] = str(row.get("media_id") or media_id)
        row["labels"] = ",".join(labels)
        normalized_segments.append(row)
    return {
        "format": "leapfrog.segment.sidecar/v1",
        "media_id": str(payload.get("media_id") or media_id),
        "title": str(payload.get("title") or ""),
        "external_ids": dict(payload.get("external_ids") or {}),
        "segments": normalized_segments,
    }


async def _normalize_payload_from_external(path: Path, *, media_id: str, title: str) -> dict[str, Any]:
    segments = await read_external_segments(path, default_media_id=media_id)
    rows: list[dict[str, Any]] = []
    for segment in segments:
        labels = _split_labels(segment.labels)
        if not labels:
            inferred = _find_leaf_key_from_text(segment.text_excerpt or "")
            if inferred:
                labels = [inferred]
        if not labels or not all(get_vidangel_leaf_metadata(label) for label in labels):
            raise RewriteEligibilityError("External skip file contains segments without exact VidAngel leaf keys.")
        rows.append(
            {
                "media_id": segment.media_id,
                "start_time": segment.start_time,
                "end_time": segment.end_time,
                "category": segment.category,
                "source": segment.source,
                "confidence": segment.confidence,
                "labels": ",".join(labels),
                "text_excerpt": segment.text_excerpt,
                "review_status": segment.review_status,
            }
        )
    return {
        "format": "leapfrog.segment.sidecar/v1",
        "media_id": media_id,
        "title": title,
        "external_ids": {},
        "segments": rows,
    }


async def _normalize_payload_from_skp(path: Path, *, media_id: str, title: str) -> dict[str, Any]:
    raw = await asyncio.to_thread(path.read_text, "utf-8")
    segments = parse_skp_text(raw)
    rows: list[dict[str, Any]] = []
    for segment in segments:
        inferred = _find_leaf_key_from_text(segment.text_excerpt or "")
        if not inferred:
            raise RewriteEligibilityError("SKP timestamps could not be mapped to exact VidAngel leaf keys.")
        metadata = get_vidangel_leaf_metadata(inferred) or {}
        rows.append(
            {
                "media_id": media_id,
                "start_time": segment.start_time,
                "end_time": segment.end_time,
                "category": str(metadata.get("category") or ""),
                "source": "skp",
                "confidence": None,
                "labels": inferred,
                "text_excerpt": segment.text_excerpt,
                "review_status": "pending",
            }
        )
    return {
        "format": "leapfrog.segment.sidecar/v1",
        "media_id": media_id,
        "title": title,
        "external_ids": {"import_source": "skp"},
        "segments": rows,
    }


async def normalize_rewrite_input(
    *,
    file_path: str,
    media_id: str,
    title: str,
) -> tuple[dict[str, Any], str, str]:
    """Return normalized sidecar payload, source path, and source type."""
    sidecar_path = await _find_adjacent_rewrite_source(file_path)
    if sidecar_path is None:
        raise RewriteEligibilityError("No adjacent sidecar or SKP file was found.")
    suffix = sidecar_path.suffix.lower()
    if suffix == ".skp":
        payload = await _normalize_payload_from_skp(sidecar_path, media_id=media_id, title=title)
        return payload, str(sidecar_path), "skp-normalized"
    if suffix == ".json":
        payload = await _normalize_payload_from_sidecar(sidecar_path, media_id=media_id)
        return payload, str(sidecar_path), "sidecar"
    payload = await _normalize_payload_from_external(sidecar_path, media_id=media_id, title=title)
    return payload, str(sidecar_path), "sidecar"


async def _find_adjacent_rewrite_source(file_path: str) -> Path | None:
    sidecar = None
    for candidate in candidate_sidecar_paths(file_path):
        if await asyncio.to_thread(candidate.exists):
            sidecar = candidate
            break
    if sidecar is not None:
        return sidecar
    return await asyncio.to_thread(_adjacent_skp_path, file_path)


async def get_rewrite_settings() -> dict[str, Any]:
    raw_leaf_keys = await db.get_setting(REWRITE_SELECTED_LEAF_KEYS, "[]")
    raw_language_mode = await db.get_setting(REWRITE_LANGUAGE_MODE_KEY, REWRITE_LANGUAGE_MODE_DEFAULT)
    raw_profile_user = await db.get_setting(REWRITE_PROFILE_USER_KEY, "")
    raw_size_limit = await db.get_setting(REWRITE_SIZE_LIMIT_KEY, str(REWRITE_SIZE_LIMIT_PERCENT))
    try:
        selected_leaf_keys = [str(value).strip() for value in json.loads(raw_leaf_keys) if str(value).strip()]
    except Exception:
        selected_leaf_keys = []
    return {
        "selected_leaf_keys": selected_leaf_keys,
        "language_mode": raw_language_mode if raw_language_mode in {"mute", "cut"} else REWRITE_LANGUAGE_MODE_DEFAULT,
        "profile_user": str(raw_profile_user or "").strip(),
        "size_limit_percent": int(raw_size_limit or REWRITE_SIZE_LIMIT_PERCENT),
        "output_container": REWRITE_OUTPUT_CONTAINER,
        "output_strategy": "sibling",
    }


async def update_rewrite_settings(
    *,
    selected_leaf_keys: list[str] | None = None,
    language_mode: str | None = None,
    profile_user: str | None = None,
) -> dict[str, Any]:
    updates: dict[str, str] = {}
    if selected_leaf_keys is not None:
        updates[REWRITE_SELECTED_LEAF_KEYS] = json.dumps(sorted({str(value).strip() for value in selected_leaf_keys if str(value).strip()}))
    if language_mode is not None:
        updates[REWRITE_LANGUAGE_MODE_KEY] = language_mode if language_mode in {"mute", "cut"} else REWRITE_LANGUAGE_MODE_DEFAULT
    if profile_user is not None:
        updates[REWRITE_PROFILE_USER_KEY] = str(profile_user or "").strip()
    if updates:
        await db.update_settings(updates)
    return await get_rewrite_settings()


async def get_leaf_keys_for_user_profile(user_id: str) -> list[str]:
    """Resolve enabled VidAngel leaf keys for one saved user profile."""
    category_preferences = build_user_category_preferences_map(
        await db.get_all_user_category_preferences()
    )
    label_preferences = build_user_label_preferences_map(
        await db.get_all_user_label_preferences()
    )
    overall_filters = build_user_filter_map(await db.get_all_user_filters())
    threshold_defaults = await get_preference_threshold_settings()
    default_skip_labels = await get_default_skip_label_settings()
    category_metadata = await get_category_metadata()
    label_definitions_by_category = {
        str(category["key"]): [dict(label) for label in category.get("labels", [])]
        for category in category_metadata
    }
    resolved = resolve_preferences_for_users(
        [user_id],
        overall_filters=overall_filters,
        stored_preferences_by_user=category_preferences,
        threshold_defaults=threshold_defaults,
        stored_label_preferences_by_user=label_preferences,
        label_definitions_by_category=label_definitions_by_category,
        default_skip_labels=default_skip_labels,
    ).get(user_id, {})
    selected: list[str] = []
    for category, preference in resolved.items():
        if not preference.get("enabled"):
            continue
        for leaf_key, leaf_preference in (preference.get("labels") or {}).items():
            if leaf_preference.get("enabled"):
                selected.append(str(leaf_key))
    return sorted(set(selected))


async def discover_rewrite_candidates() -> list[dict[str, Any]]:
    """Return scan jobs plus rewrite readiness metadata."""
    jobs = await db.get_scan_jobs()
    persisted_roots = await _get_persisted_rewrite_discovery_roots()
    discovery_roots = _minimal_root_set([*_known_media_roots(jobs), *persisted_roots])
    candidates: list[dict[str, Any]] = []
    seen_file_paths: set[str] = set()
    for job in jobs:
        file_path = str(job.get("file_path") or "").strip()
        if not file_path:
            continue
        seen_file_paths.add(file_path)
        candidates.append(await _build_candidate_record(file_path=file_path, job=job))
    for media_path, source_path in await _discover_rewrite_sources_with_cache(discovery_roots):
        file_path = str(media_path)
        if file_path in seen_file_paths:
            continue
        seen_file_paths.add(file_path)
        candidates.append(await _build_candidate_record(file_path=file_path, source_path=source_path))
    return sorted(candidates, key=lambda item: str(item["title"]).lower())


def _merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted((max(0.0, start), max(start, end)) for start, end in ranges if end > start):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _subtract_ranges(
    ranges: list[tuple[float, float]],
    subtractors: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    remaining = list(ranges)
    for cut_start, cut_end in subtractors:
        next_ranges: list[tuple[float, float]] = []
        for start, end in remaining:
            if cut_end <= start or cut_start >= end:
                next_ranges.append((start, end))
                continue
            if cut_start > start:
                next_ranges.append((start, cut_start))
            if cut_end < end:
                next_ranges.append((cut_end, end))
        remaining = next_ranges
    return _merge_ranges(remaining)


def _invert_ranges(duration_s: float, cut_ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    keep_ranges: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in _merge_ranges(cut_ranges):
        if start > cursor:
            keep_ranges.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_s:
        keep_ranges.append((cursor, duration_s))
    return _merge_ranges(keep_ranges)


def _intersections(base: tuple[float, float], ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    start, end = base
    result: list[tuple[float, float]] = []
    for other_start, other_end in ranges:
        overlap_start = max(start, other_start)
        overlap_end = min(end, other_end)
        if overlap_end > overlap_start:
            result.append((overlap_start - start, overlap_end - start))
    return result


def _normalize_media_info(media_info: dict[str, Any]) -> dict[str, Any]:
    audio_streams = [
        {
            "index": int(stream.get("index")),
            "language": str(stream.get("language") or ""),
            "title": str(stream.get("title") or ""),
            "codec_name": str(stream.get("codec_name") or ""),
            "is_english": bool(stream.get("is_english")),
        }
        for stream in media_info.get("audio_streams", [])
        if stream.get("index") is not None
    ]
    subtitle_streams = [
        {
            "index": int(stream.get("index")),
            "language": str(stream.get("language") or ""),
            "title": str(stream.get("title") or ""),
            "codec_name": str(stream.get("codec_name") or ""),
            "is_english": bool(stream.get("is_english")),
            "is_text": bool(stream.get("is_text")),
        }
        for stream in media_info.get("subtitle_streams", [])
        if stream.get("index") is not None
    ]
    return {
        "duration_s": float(media_info.get("duration_s") or 0.0),
        "bitrate": int(float(media_info.get("bitrate") or 0)),
        "video_stream_index": int(media_info.get("video_stream_index") or 0),
        "audio_streams": audio_streams,
        "subtitle_streams": subtitle_streams,
        "has_audio": bool(audio_streams),
    }


def _subtitle_stream_needs_rewrite(
    stream: dict[str, Any],
    *,
    cut_ranges: list[tuple[float, float]],
    has_english_redactions: bool,
) -> bool:
    return bool(cut_ranges) or bool(stream.get("is_english") and has_english_redactions)


def _validate_subtitle_stream_support(
    subtitle_streams: list[dict[str, Any]],
    *,
    cut_ranges: list[tuple[float, float]],
    has_english_redactions: bool,
) -> None:
    for stream in subtitle_streams:
        if _subtitle_stream_needs_rewrite(stream, cut_ranges=cut_ranges, has_english_redactions=has_english_redactions) and not stream.get("is_text"):
            raise RewriteEligibilityError(
                "Subtitle stream rewrite currently supports only text subtitle codecs when cuts or English-language muting are required."
            )


def _map_time_after_cuts(time_s: float, cut_ranges: list[tuple[float, float]]) -> float:
    removed = 0.0
    for start, end in cut_ranges:
        if time_s <= start:
            break
        removed += min(time_s, end) - start
    return max(0.0, time_s - removed)


def _format_srt_time(total_ms: int) -> str:
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _serialize_subtitle_cues(cues: list[SubtitleCue]) -> str:
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{_format_srt_time(cue.start_ms)} --> {_format_srt_time(cue.end_ms)}",
                    cue.text,
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _mask_text(text: str) -> str:
    return re.sub(r"[A-Za-z0-9]", "*", text)


def _phrase_tokens(text: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", str(text or "").lower()) if token]


def _subtitle_redaction_terms(labels: list[str], text_excerpt: str | None) -> list[str]:
    terms: list[str] = []
    for label in labels:
        leaf_key = str(label or "").strip()
        if not leaf_key:
            continue
        terms.append(leaf_key.replace("_", " "))
    raw_excerpt = str(text_excerpt or "").strip()
    if raw_excerpt:
        terms.append(raw_excerpt)
    # Keep source order stable while removing empty/duplicate entries.
    return list(dict.fromkeys(term for term in terms if term.strip()))


def _token_stems(token: str) -> list[str]:
    stems = {token}
    if token.endswith("ies") and len(token) > 4:
        stems.add(f"{token[:-3]}y")
    for suffix in ("ing", "ed", "ers", "er", "es", "s"):
        if not token.endswith(suffix) or len(token) - len(suffix) < 3:
            continue
        stem = token[:-len(suffix)]
        stems.add(stem)
        # Caption wording often shifts between base, past tense, and gerund forms.
        if suffix in {"ing", "ed"} and stem and not stem.endswith("e"):
            stems.add(f"{stem}e")
        if suffix == "ing" and len(stem) >= 2 and stem[-1] == stem[-2]:
            stems.add(stem[:-1])
    return sorted(stems, key=len, reverse=True)


def _token_pattern(token: str) -> str:
    if len(token) <= 3:
        return re.escape(token)
    variants: list[str] = []
    for stem in _token_stems(token):
        if len(stem) <= 3:
            variants.append(re.escape(stem))
            continue
        if stem.endswith("y") and len(stem) > 3:
            variants.append(rf"(?:{re.escape(stem)}|{re.escape(stem[:-1])}ies)")
            continue
        variants.append(rf"{re.escape(stem)}(?:s|es|ed|ing|er|ers)?")
    deduped = list(dict.fromkeys(variants))
    return "(?:" + "|".join(deduped) + ")"


def _phrase_pattern(phrase: str) -> re.Pattern[str] | None:
    tokens = _phrase_tokens(phrase)
    if not tokens:
        return None
    if len(tokens) == 1:
        return re.compile(rf"\b{_token_pattern(tokens[0])}\b", re.IGNORECASE)
    separator = r"[\W_]*"
    return re.compile(rf"\b{separator.join(_token_pattern(token) for token in tokens)}\b", re.IGNORECASE)


def _redact_subtitle_text(text: str, phrases: list[str]) -> str:
    redacted = text
    for phrase in sorted({phrase.strip() for phrase in phrases if phrase and phrase.strip()}, key=len, reverse=True):
        pattern = _phrase_pattern(phrase)
        if pattern is None:
            continue
        redacted = pattern.sub(lambda match: _mask_text(match.group(0)), redacted)
    return redacted


def _rewrite_subtitle_cues(
    cues: list[SubtitleCue],
    *,
    keep_ranges: list[tuple[float, float]],
    cut_ranges: list[tuple[float, float]],
    subtitle_redactions: list[dict[str, Any]],
) -> list[SubtitleCue]:
    rewritten: list[SubtitleCue] = []
    for cue in cues:
        cue_start = cue.start_ms / 1000.0
        cue_end = cue.end_ms / 1000.0
        if cue_end <= cue_start:
            continue
        matching_phrases = [
            term
            for redaction in subtitle_redactions
            if float(redaction.get("end") or 0.0) > cue_start and float(redaction.get("start") or 0.0) < cue_end
            for term in (
                [str(redaction.get("text") or "").strip()]
                if redaction.get("text") is not None
                else [str(value).strip() for value in redaction.get("terms", [])]
            )
            if term
        ]
        rewritten_text = _redact_subtitle_text(cue.text, matching_phrases)
        for visible_start, visible_end in _intersections((cue_start, cue_end), keep_ranges):
            absolute_start = cue_start + visible_start
            absolute_end = cue_start + visible_end
            mapped_start = int(round(_map_time_after_cuts(absolute_start, cut_ranges) * 1000))
            mapped_end = int(round(_map_time_after_cuts(absolute_end, cut_ranges) * 1000))
            if mapped_end <= mapped_start:
                continue
            rewritten.append(
                SubtitleCue(
                    start_ms=mapped_start,
                    end_ms=mapped_end,
                    text=rewritten_text,
                )
            )
    return rewritten


async def _extract_subtitle_stream_as_srt(file_path: str, stream_index: int) -> list[SubtitleCue]:
    cmd = [
        _FFMPEG_BIN,
        "-loglevel",
        "error",
        "-i",
        file_path,
        "-map",
        f"0:{stream_index}",
        "-f",
        "srt",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RewriteExecutionError(stderr.decode("utf-8", errors="replace")[:500] or "subtitle extraction failed")
    return parse_subtitle_text(stdout.decode("utf-8", errors="ignore"))


async def _detect_preferred_video_encoder() -> str | None:
    global _preferred_video_encoder_cache
    if _preferred_video_encoder_cache is not False:
        return _preferred_video_encoder_cache or None
    cmd = [_FFMPEG_BIN, "-hide_banner", "-encoders"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            output = stdout.decode("utf-8", errors="ignore")
            for encoder in _GPU_ENCODER_CANDIDATES:
                if encoder in output:
                    _preferred_video_encoder_cache = encoder
                    return encoder
    except Exception:
        logger.debug("Could not query ffmpeg encoders for rewrite GPU acceleration", exc_info=True)
    _preferred_video_encoder_cache = None
    return None


async def _probe_media(file_path: str) -> dict[str, Any]:
    cmd = [
        _FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "format=duration,bit_rate:stream=index,codec_type,codec_name:stream_tags=language,title",
        "-of", "json",
        file_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RewriteExecutionError(stderr.decode("utf-8", errors="replace")[:500] or "ffprobe failed")
    payload = json.loads(stdout.decode("utf-8"))
    duration = float(payload.get("format", {}).get("duration") or 0.0)
    bitrate = int(float(payload.get("format", {}).get("bit_rate") or 0))
    video_stream_index = 0
    audio_streams: list[dict[str, Any]] = []
    subtitle_streams: list[dict[str, Any]] = []
    for stream in payload.get("streams", []):
        codec_type = str(stream.get("codec_type") or "")
        tags = dict(stream.get("tags") or {})
        record = {
            "index": int(stream.get("index") or 0),
            "language": str(tags.get("language") or ""),
            "title": str(tags.get("title") or ""),
            "codec_name": str(stream.get("codec_name") or ""),
        }
        if codec_type == "video" and video_stream_index == 0:
            video_stream_index = record["index"]
        elif codec_type == "audio":
            record["is_english"] = _is_english_stream(record)
            audio_streams.append(record)
        elif codec_type == "subtitle":
            record["is_english"] = _is_english_stream(record)
            record["is_text"] = str(record["codec_name"]).lower() in _TEXT_SUBTITLE_CODECS
            subtitle_streams.append(record)
    return _normalize_media_info(
        {
            "duration_s": duration,
            "bitrate": bitrate,
            "video_stream_index": video_stream_index,
            "audio_streams": audio_streams,
            "subtitle_streams": subtitle_streams,
        }
    )


async def build_rewrite_plan(
    media_id: str,
    *,
    selected_leaf_keys: list[str],
    language_mode: str,
) -> dict[str, Any]:
    file_path, title, job = await _resolve_media_record(media_id)
    payload, sidecar_path, source_type = await normalize_rewrite_input(
        file_path=file_path,
        media_id=str((job or {}).get("plex_guid") or media_id),
        title=title,
    )
    leaf_keys = sorted({str(value).strip() for value in selected_leaf_keys if str(value).strip()})
    if not leaf_keys:
        raise RewriteEligibilityError("At least one VidAngel leaf filter must be selected.")
    invalid = [leaf_key for leaf_key in leaf_keys if not get_vidangel_leaf_metadata(leaf_key)]
    if invalid:
        raise RewriteEligibilityError(f"Unknown VidAngel leaf filter(s): {', '.join(invalid)}")
    media_info = _normalize_media_info(await _probe_media(file_path))
    duration_s = media_info["duration_s"]
    cut_ranges: list[tuple[float, float]] = []
    mute_ranges: list[tuple[float, float]] = []
    subtitle_redactions: list[dict[str, Any]] = []
    matched_leaf_keys: set[str] = set()
    matched_segment_count = 0

    normalized_segments = normalize_sidecar_segments(payload, default_media_id=media_id)
    for segment in normalized_segments:
        labels = [label for label in _split_labels(segment.labels) if label in leaf_keys]
        if not labels:
            continue
        matched_segment_count += 1
        matched_leaf_keys.update(labels)
        action = "cut"
        if all(_is_language_leaf(label) for label in labels):
            action = "mute" if language_mode == "mute" else "cut"
        if action == "cut":
            cut_ranges.append((segment.start_time, segment.end_time))
        else:
            mute_ranges.append((segment.start_time, segment.end_time))
            redaction_terms = _subtitle_redaction_terms(labels, segment.text_excerpt)
            if redaction_terms:
                subtitle_redactions.append(
                    {
                        "start": segment.start_time,
                        "end": segment.end_time,
                        "terms": redaction_terms,
                    }
                )

    cut_ranges = _merge_ranges(cut_ranges)
    mute_ranges = _subtract_ranges(_merge_ranges(mute_ranges), cut_ranges)
    keep_ranges = _invert_ranges(duration_s, cut_ranges)
    _validate_subtitle_stream_support(
        media_info["subtitle_streams"],
        cut_ranges=cut_ranges,
        has_english_redactions=bool(subtitle_redactions),
    )
    cut_duration = sum(end - start for start, end in cut_ranges)
    mute_duration = sum(end - start for start, end in mute_ranges)
    kept_duration = sum(end - start for start, end in keep_ranges)
    muted_audio_stream_indexes = [
        int(stream["index"])
        for stream in media_info["audio_streams"]
        if stream.get("is_english") and mute_ranges
    ]
    muted_subtitle_stream_indexes = [
        int(stream["index"])
        for stream in media_info["subtitle_streams"]
        if stream.get("is_english") and mute_ranges
    ]

    return {
        "media_id": media_id,
        "title": title,
        "file_path": file_path,
        "sidecar_path": sidecar_path,
        "source_type": source_type,
        "selected_leaf_keys": leaf_keys,
        "matched_leaf_keys": sorted(matched_leaf_keys),
        "matched_segment_count": matched_segment_count,
        "language_mode": language_mode,
        "cut_ranges": [{"start": start, "end": end} for start, end in cut_ranges],
        "mute_ranges": [{"start": start, "end": end} for start, end in mute_ranges],
        "keep_ranges": [{"start": start, "end": end} for start, end in keep_ranges],
        "muted_audio_stream_indexes": muted_audio_stream_indexes,
        "muted_subtitle_stream_indexes": muted_subtitle_stream_indexes,
        "subtitle_redactions": subtitle_redactions,
        "summary": {
            "cut_duration_seconds": round(cut_duration, 3),
            "mute_duration_seconds": round(mute_duration, 3),
            "kept_duration_seconds": round(kept_duration, 3),
            "output_path": str(_rewrite_output_path(file_path)),
        },
        "media_info": media_info,
    }


def _build_filter_complex(
    keep_ranges: list[tuple[float, float]],
    mute_ranges: list[tuple[float, float]],
    *,
    video_stream_index: int,
    audio_streams: list[dict[str, Any]],
) -> tuple[str, str, list[str]]:
    parts: list[str] = []
    video_labels: list[str] = []
    for index, keep_range in enumerate(keep_ranges):
        start, end = keep_range
        video_label = f"v{index}"
        parts.append(f"[0:{video_stream_index}]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[{video_label}]")
        video_labels.append(f"[{video_label}]")
    parts.append(f"{''.join(video_labels)}concat=n={len(keep_ranges)}:v=1:a=0[vout]")
    audio_output_labels: list[str] = []
    for stream_position, stream in enumerate(audio_streams):
        segment_labels: list[str] = []
        for index, keep_range in enumerate(keep_ranges):
            start, end = keep_range
            audio_segment_label = f"a{stream_position}_{index}"
            audio_chain = f"[0:{int(stream['index'])}]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS"
            if stream.get("is_english"):
                for mute_start, mute_end in _intersections(keep_range, mute_ranges):
                    audio_chain += f",volume=enable='between(t,{mute_start:.3f},{mute_end:.3f})':volume=0"
            audio_chain += f"[{audio_segment_label}]"
            parts.append(audio_chain)
            segment_labels.append(f"[{audio_segment_label}]")
        output_label = f"aout{stream_position}"
        parts.append(f"{''.join(segment_labels)}concat=n={len(keep_ranges)}:v=0:a=1[{output_label}]")
        audio_output_labels.append(output_label)
    return ";".join(parts), "vout", audio_output_labels


def _target_video_bitrate(plan: dict[str, Any], *, source_size_bytes: int, size_limit_percent: int) -> int:
    media_info = _normalize_media_info(plan["media_info"])
    duration_s = float(media_info["duration_s"] or 0.0)
    if duration_s <= 0:
        return 2_000_000
    max_size_bytes = int(source_size_bytes * (size_limit_percent / 100.0))
    total_bitrate = int((max_size_bytes * 8) / max(duration_s, 1.0))
    audio_budget = 192_000 * len(media_info["audio_streams"])
    return max(400_000, total_bitrate - audio_budget)


def _video_encoder_options(encoder: str, video_bitrate: int) -> list[str]:
    options = [
        "-c:v", encoder,
        "-b:v", str(video_bitrate),
        "-maxrate", str(int(video_bitrate * 1.15)),
        "-bufsize", str(video_bitrate * 2),
    ]
    if encoder == "libx264":
        options.extend(["-preset", "medium"])
    elif encoder == "h264_nvenc":
        options.extend(["-preset", "p4"])
    return options


async def _prepare_subtitle_streams(
    *,
    source_path: str,
    media_info: dict[str, Any],
    keep_ranges: list[tuple[float, float]],
    cut_ranges: list[tuple[float, float]],
    subtitle_redactions: list[dict[str, Any]],
    temp_dir: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rewritten_subtitles: list[dict[str, Any]] = []
    passthrough_subtitles: list[dict[str, Any]] = []
    for position, stream in enumerate(media_info["subtitle_streams"]):
        stream_redactions = subtitle_redactions if stream.get("is_english") else []
        if not _subtitle_stream_needs_rewrite(stream, cut_ranges=cut_ranges, has_english_redactions=bool(stream_redactions)):
            passthrough_subtitles.append(stream)
            continue
        cues = await _extract_subtitle_stream_as_srt(source_path, int(stream["index"]))
        rewritten_cues = _rewrite_subtitle_cues(
            cues,
            keep_ranges=keep_ranges,
            cut_ranges=cut_ranges,
            subtitle_redactions=stream_redactions,
        )
        if not rewritten_cues:
            continue
        subtitle_path = Path(temp_dir) / f"subtitle-{position}.srt"
        await asyncio.to_thread(subtitle_path.write_text, _serialize_subtitle_cues(rewritten_cues), "utf-8")
        rewritten_subtitles.append(
            {
                "path": str(subtitle_path),
                "language": str(stream.get("language") or ""),
                "title": str(stream.get("title") or ""),
            }
        )
    return rewritten_subtitles, passthrough_subtitles


async def _run_ffmpeg(
    *,
    source_path: str,
    output_path: str,
    keep_ranges: list[tuple[float, float]],
    mute_ranges: list[tuple[float, float]],
    media_info: dict[str, Any],
    rewritten_subtitles: list[dict[str, Any]],
    passthrough_subtitles: list[dict[str, Any]],
    video_bitrate: int,
) -> str:
    filter_complex, video_output_label, audio_output_labels = _build_filter_complex(
        keep_ranges,
        mute_ranges,
        video_stream_index=int(media_info["video_stream_index"]),
        audio_streams=media_info["audio_streams"],
    )
    preferred_encoder = await _detect_preferred_video_encoder()
    encoders_to_try = [preferred_encoder] if preferred_encoder else []
    if "libx264" not in encoders_to_try:
        encoders_to_try.append("libx264")
    last_error = "ffmpeg failed"
    for encoder in encoders_to_try:
        cmd = [
            _FFMPEG_BIN,
            "-y",
            "-i", source_path,
        ]
        for subtitle in rewritten_subtitles:
            cmd += ["-i", subtitle["path"]]
        cmd += [
            "-filter_complex", filter_complex,
            "-map", f"[{video_output_label}]",
        ]
        for audio_label in audio_output_labels:
            cmd += ["-map", f"[{audio_label}]"]
        for subtitle_input_index, _ in enumerate(rewritten_subtitles, start=1):
            cmd += ["-map", f"{subtitle_input_index}:0"]
        for stream in passthrough_subtitles:
            cmd += ["-map", f"0:{int(stream['index'])}"]
        cmd += [
            "-map_metadata", "0",
            *_video_encoder_options(encoder, video_bitrate),
        ]
        if audio_output_labels:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        subtitle_stream_count = len(rewritten_subtitles) + len(passthrough_subtitles)
        if subtitle_stream_count:
            cmd += ["-c:s", "copy"]
            for subtitle_index in range(len(rewritten_subtitles)):
                cmd += [f"-c:s:{subtitle_index}", "srt"]
        for audio_index, stream in enumerate(media_info["audio_streams"]):
            if stream.get("language"):
                cmd += [f"-metadata:s:a:{audio_index}", f"language={stream['language']}"]
            if stream.get("title"):
                cmd += [f"-metadata:s:a:{audio_index}", f"title={stream['title']}"]
        subtitle_metadata_streams = [*rewritten_subtitles, *passthrough_subtitles]
        for subtitle_index, stream in enumerate(subtitle_metadata_streams):
            if stream.get("language"):
                cmd += [f"-metadata:s:s:{subtitle_index}", f"language={stream['language']}"]
            if stream.get("title"):
                cmd += [f"-metadata:s:s:{subtitle_index}", f"title={stream['title']}"]
        cmd.append(output_path)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            return encoder
        last_error = stderr.decode("utf-8", errors="replace")[:2000] or "ffmpeg failed"
        if encoder != "libx264":
            logger.warning("Rewrite hardware encoder %s failed, retrying with libx264: %s", encoder, last_error[:200])
    raise RewriteExecutionError(last_error)


async def execute_rewrite_plan(
    plan: dict[str, Any],
    *,
    size_limit_percent: int,
) -> dict[str, Any]:
    source_path = str(plan["file_path"])
    output_path = str(plan["summary"]["output_path"])
    keep_ranges = [(float(row["start"]), float(row["end"])) for row in plan["keep_ranges"]]
    mute_ranges = [(float(row["start"]), float(row["end"])) for row in plan["mute_ranges"]]
    cut_ranges = [(float(row["start"]), float(row["end"])) for row in plan["cut_ranges"]]
    subtitle_redactions = [dict(row) for row in plan.get("subtitle_redactions", []) if isinstance(row, dict)]
    media_info = _normalize_media_info(plan["media_info"])
    if not keep_ranges:
        raise RewriteExecutionError("The selected filters would remove the entire file.")
    _validate_subtitle_stream_support(
        media_info["subtitle_streams"],
        cut_ranges=cut_ranges,
        has_english_redactions=bool(subtitle_redactions),
    )
    source_size = os.path.getsize(source_path)
    max_size = int(source_size * (size_limit_percent / 100.0))
    target_bitrate = _target_video_bitrate(plan, source_size_bytes=source_size, size_limit_percent=size_limit_percent)

    # Use a sibling temp directory so the final replace stays atomic on the same filesystem.
    with TemporaryDirectory(prefix="leapfrog-rewrite-", dir=str(Path(output_path).parent)) as temp_dir:
        rewritten_subtitles, passthrough_subtitles = await _prepare_subtitle_streams(
            source_path=source_path,
            media_info=media_info,
            keep_ranges=keep_ranges,
            cut_ranges=cut_ranges,
            subtitle_redactions=subtitle_redactions,
            temp_dir=temp_dir,
        )
        temp_output = str(Path(temp_dir) / Path(output_path).name)
        encoder_used = await _run_ffmpeg(
            source_path=source_path,
            output_path=temp_output,
            keep_ranges=keep_ranges,
            mute_ranges=mute_ranges,
            media_info=media_info,
            rewritten_subtitles=rewritten_subtitles,
            passthrough_subtitles=passthrough_subtitles,
            video_bitrate=target_bitrate,
        )
        result_size = os.path.getsize(temp_output)
        if result_size > max_size:
            tighter_bitrate = max(300_000, int(target_bitrate * 0.82))
            encoder_used = await _run_ffmpeg(
                source_path=source_path,
                output_path=temp_output,
                keep_ranges=keep_ranges,
                mute_ranges=mute_ranges,
                media_info=media_info,
                rewritten_subtitles=rewritten_subtitles,
                passthrough_subtitles=passthrough_subtitles,
                video_bitrate=tighter_bitrate,
            )
            result_size = os.path.getsize(temp_output)
        if result_size > max_size:
            raise RewriteExecutionError("Could not meet the configured 120% size ceiling.")
        await asyncio.to_thread(shutil.move, temp_output, output_path)

    manifest = {
        "source_file": source_path,
        "output_file": output_path,
        "selected_leaf_keys": plan["selected_leaf_keys"],
        "language_mode": plan["language_mode"],
        "cut_ranges": plan["cut_ranges"],
        "mute_ranges": plan["mute_ranges"],
        "source_size_bytes": source_size,
        "output_size_bytes": os.path.getsize(output_path),
        "ffmpeg_profile": {
            "container": REWRITE_OUTPUT_CONTAINER,
            "size_limit_percent": size_limit_percent,
            "video_encoder": encoder_used,
            "audio_stream_count": len(media_info["audio_streams"]),
            "subtitle_stream_count": len(media_info["subtitle_streams"]),
        },
    }
    manifest_path = f"{output_path}.rewrite.json"
    await asyncio.to_thread(Path(manifest_path).write_text, json.dumps(manifest, indent=2) + "\n", "utf-8")
    return {
        "media_id": plan["media_id"],
        "title": plan["title"],
        "output_path": output_path,
        "manifest_path": manifest_path,
        "source_size_bytes": source_size,
        "output_size_bytes": os.path.getsize(output_path),
        "selected_leaf_keys": plan["selected_leaf_keys"],
        "language_mode": plan["language_mode"],
        "cut_ranges": plan["cut_ranges"],
        "mute_ranges": plan["mute_ranges"],
    }
