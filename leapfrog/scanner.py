"""Background media scanner orchestration for detector pipelines."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from . import database as db
from .detectors import (
    DrugsDetector,
    NudityDetector,
    ProfanityDetector,
    SexualContentDetector,
    ViolenceDetector,
)
from .detectors.nudity import ensure_640m_model_async, ensure_local_640m_model
from .detectors.semantic import ensure_semantic_model_async, get_semantic_backend
from .domain import DETECTOR_STAGE_KEYS, MediaScanTarget, MediaSegment, SUPPORTED_CATEGORIES, get_vidangel_leaf_metadata
from .frame_extractor import get_duration_ms, sample_video_frames
from .logger import get_logger
from .server_runtime import get_client

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

_paused: bool = False
_current_guids: set[str] = set()
_queue_wakeup_event: asyncio.Event = asyncio.Event()
_skip_requested_guids: set[str] = set()
_worker_pool_size: int = 1
_restart_requested: bool = False
_queue_size_snapshot: int = 0

_state_lock = asyncio.Lock()


def get_queue_size() -> int:
    return _queue_size_snapshot


async def _refresh_queue_size_snapshot() -> None:
    global _queue_size_snapshot
    _queue_size_snapshot = len(await db.get_queue_snapshot())


def get_worker_pool_size() -> int:
    return _worker_pool_size


async def request_scanner_restart() -> None:
    """Signal scanner to restart worker pool with updated config."""
    global _restart_requested
    _restart_requested = True
    _queue_wakeup_event.set()
    logger.info("Scanner restart requested")


def get_current_scan() -> str | None:
    if not _current_guids:
        return None
    return sorted(_current_guids)[0]


def get_current_scans() -> list[str]:
    return sorted(_current_guids)


def pause_scanner() -> None:
    global _paused
    _paused = True
    logger.info("Scanner paused")


def resume_scanner() -> None:
    global _paused
    was_paused = _paused
    _paused = False
    _queue_wakeup_event.set()
    if was_paused:
        logger.info("Scanner resumed")


def is_paused() -> bool:
    return _paused


def _should_auto_pause_for_scan_window(in_window: bool) -> bool:
    return not in_window and not _current_guids


async def force_scan_job(plex_guid: str) -> None:
    """Prioritize a title for immediate scanning in the persisted queue."""
    await db.set_force_scan(plex_guid, True)
    async with _state_lock:
        if plex_guid in _current_guids:
            logger.info("Force scan requested for %s, already scanning", plex_guid)
            return
    queued = await db.queue_scan_job(
        plex_guid,
        to_front=True,
        priority=100,
        reason="force_scan",
    )
    if queued:
        await _refresh_queue_size_snapshot()
        _queue_wakeup_event.set()
    logger.warning("Force scan activated for %s — will scan immediately", plex_guid)


async def enqueue(plex_guid: str) -> None:
    async with _state_lock:
        if plex_guid in _current_guids:
            logger.debug("Skipping enqueue for %s: already scanning", plex_guid)
            return
    queued = await db.queue_scan_job(plex_guid, to_front=False, priority=0, reason="scan")
    if queued:
        await _refresh_queue_size_snapshot()
        _queue_wakeup_event.set()


async def enqueue_pending(media_type: str | None = None, *, force: bool = False) -> int:
    """Rebuild the persisted queue order for pending scan jobs and return queued count."""
    jobs = await db.get_scan_jobs(status="pending")
    active = [job for job in jobs if not job.get("ignored")]
    if media_type in {"movie", "episode"}:
        active = [job for job in active if job.get("media_type") == media_type]

    movies = [job for job in active if job.get("media_type") != "episode"]
    episodes = [job for job in active if job.get("media_type") == "episode"]

    movies.sort(
        key=lambda job: (
            _content_rating_scan_priority(job),
            int(job.get("rating_key") or 0),
        ),
        reverse=True,
    )

    show_buckets: dict[str, list[dict]] = defaultdict(list)
    for episode in episodes:
        bucket_key = episode.get("show_guid") or episode.get("title", "").split(" – ")[0]
        show_buckets[str(bucket_key)].append(episode)

    for bucket in show_buckets.values():
        bucket.sort(key=lambda job: int(job.get("rating_key") or 0))

    ordered_shows = sorted(
        show_buckets.values(),
        key=lambda eps: (
            max(_content_rating_scan_priority(job) for job in eps),
            max(int(job.get("rating_key") or 0) for job in eps),
        ),
        reverse=True,
    )

    ordered = movies + [episode for bucket in ordered_shows for episode in bucket]
    queued_guids = [job["plex_guid"] for job in ordered if job["plex_guid"] not in _current_guids]
    if force:
        for plex_guid in reversed(queued_guids):
            await force_scan_job(plex_guid)
    else:
        await db.replace_queue_snapshot(queued_guids)
    await _refresh_queue_size_snapshot()
    if queued_guids:
        logger.info(
            "Queued %d pending scan jobs (%d movies, %d episodes, %d ignored skipped)",
            len(queued_guids),
            len(movies),
            len(episodes),
            len(jobs) - len(active),
        )
    _queue_wakeup_event.set()
    return len(queued_guids)


def _ensure_local_640m_model() -> str:
    """Backward-compatible wrapper for settings validation."""
    return ensure_local_640m_model()


async def _ensure_640m_model_async() -> str:
    """Backward-compatible wrapper for scanner code paths."""
    return await ensure_640m_model_async()


def _delete_thumbnail_files(paths: list[str]) -> None:
    """Best-effort thumbnail cleanup for replaced detector output."""
    for path in {str(value).strip() for value in paths if str(value).strip()}:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("Could not delete thumbnail %s: %s", path, exc)


def _content_rating_scan_priority(job: dict) -> int:
    """Return the default scan-order priority bucket for a job."""
    rating = str(job.get("content_rating") or "").strip().upper()
    return {
        "R": 4,
        "PG-13": 3,
        "PG13": 3,
        "PG": 2,
        "G": 1,
    }.get(rating, 0)


def _cluster_frames(
    flagged_ms: list[int],
    gap_ms: int = 15_000,
    min_hits: int = 1,
) -> list[tuple[int, int]]:
    """Merge consecutive flagged frame offsets into (start_ms, end_ms) segments."""
    if not flagged_ms:
        return []
    flagged_ms = sorted(flagged_ms)
    segments = []
    start = flagged_ms[0]
    prev = flagged_ms[0]
    hit_count = 1
    for ms in flagged_ms[1:]:
        if ms - prev > gap_ms:
            if hit_count >= max(1, min_hits):
                segments.append((start, prev + gap_ms))
            start = ms
            hit_count = 1
        else:
            hit_count += 1
        prev = ms
    if hit_count >= max(1, min_hits):
        segments.append((start, prev + gap_ms))
    return segments


_NUDENET_TO_VIDANGEL = {
    "FEMALE_GENITALIA_EXPOSED": "nudity_female",
    "FEMALE_BREAST_EXPOSED": "nudity_female",
    "MALE_GENITALIA_EXPOSED": "nudity_male",
    "MALE_BREAST_EXPOSED": "nudity_male",
    "ANUS_EXPOSED": "nudity_both",
    "BUTTOCKS_EXPOSED": "nudity_both",
    "FEMALE_GENITALIA_COVERED": "immodesty_female",
    "FEMALE_BREAST_COVERED": "immodesty_female",
    "MALE_BREAST_COVERED": "immodesty_male",
    "BUTTOCKS_COVERED": "immodesty_both",
}

_SEXUAL_TO_VIDANGEL = {
    "brief_kiss": "kissing_normal",
    "romantic_kiss": "kissing_normal",
    "heavy_making_out": "kissing_passion",
    "intimate_touch": "sexually_suggestive",
    "bed_intimacy": "implied_not_shown",
    "lingerie": "sexually_suggestive",
    "striptease": "sexually_suggestive",
    "simulated_sex": "shown_w_o_nudity",
    "explicit_sex": "shown_w_nudity",
    "oral_sex": "shown_w_nudity",
    "masturbation": "shown_w_nudity",
    "sexual_touching": "shown_w_o_nudity",
}

_VIOLENCE_TO_VIDANGEL = {
    "graphic_violence": "graphic",
    "fight": "non_graphic",
    "blood": "gore",
    "weapon_threat": "non_graphic",
    "gunfire": "non_graphic",
    "stabbing": "graphic",
    "explosion": "non_graphic",
    "dead_body": "objectionable",
    "disturbing_image": "objectionable",
    "medical_injury": "medical_graphic",
}

_DRUGS_TO_VIDANGEL = {
    "hard_drug_use": "drugs_illegal",
    "needle": "drugs_illegal",
    "powder_drugs": "drugs_illegal",
    "pill_abuse": "drugs_illegal",
    "drug_paraphernalia": "drugs_implied",
    "smoking_drugs": "drugs_illegal",
    "marijuana": "drugs_illegal",
    "alcohol_abuse": "drugs_legal",
    "tobacco": "drugs_legal",
}

_SCANNER_STAGE_CATEGORY_GROUPS = {
    "nudity": {"sex_nudity_immodesty"},
    "sexual_content": {"sex_any", "kissing"},
    "profanity": {
        "language_blasphemy",
        "language_language_childish",
        "language_language_racial",
        "language_language_sexual",
        "language_profanity",
        "language_profanity_captions",
    },
    "violence": {"violence_blood_gore", "human_functions"},
    "drugs": {"alcohol_or_drug_use"},
}


def _group_for_leaf_key(leaf_key: str) -> str | None:
    metadata = get_vidangel_leaf_metadata(leaf_key)
    if not metadata:
        return None
    return str(metadata.get("category") or "").strip() or None


def _split_segment_labels(raw: str) -> list[str]:
    return [label.strip() for label in str(raw or "").split(",") if label.strip()]


def _resolve_profanity_leaf(label: str, *, source: str) -> str | None:
    token = str(label or "").strip().lower().replace(" ", "_")
    candidates: list[str] = []
    if source == "subtitles":
        candidates.append(f"{token}_caption")
    candidates.extend([token, "other_caption" if source == "subtitles" else "other_profanity"])
    for candidate in candidates:
        if _group_for_leaf_key(candidate):
            return candidate
    return None


def _map_scanner_label(stage_key: str, label: str, *, source: str) -> tuple[str, str] | None:
    leaf_key = None
    if stage_key == "nudity":
        leaf_key = _NUDENET_TO_VIDANGEL.get(label)
    elif stage_key == "sexual_content":
        leaf_key = _SEXUAL_TO_VIDANGEL.get(label)
    elif stage_key == "violence":
        leaf_key = _VIOLENCE_TO_VIDANGEL.get(label)
    elif stage_key == "drugs":
        leaf_key = _DRUGS_TO_VIDANGEL.get(label)
    elif stage_key == "profanity":
        leaf_key = _resolve_profanity_leaf(label, source=source)
    if not leaf_key:
        return None
    group = _group_for_leaf_key(leaf_key)
    if not group:
        return None
    return group, leaf_key


def _remap_detector_segments(stage_key: str, segments: list[MediaSegment]) -> list[MediaSegment]:
    remapped: list[MediaSegment] = []
    for segment in segments:
        grouped_labels: dict[str, list[str]] = {}
        for label in _split_segment_labels(segment.labels):
            mapped = _map_scanner_label(stage_key, label, source=segment.source)
            if not mapped:
                continue
            group, leaf_key = mapped
            bucket = grouped_labels.setdefault(group, [])
            if leaf_key not in bucket:
                bucket.append(leaf_key)
        if not grouped_labels:
            fallback_group = next(iter(_SCANNER_STAGE_CATEGORY_GROUPS.get(stage_key, set())), "")
            if fallback_group:
                grouped_labels[fallback_group] = []
        if not grouped_labels:
            continue
        for group, leaf_keys in grouped_labels.items():
            remapped.append(
                MediaSegment(
                    media_id=segment.media_id,
                    title=segment.title,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    category=group,
                    source=segment.source,
                    confidence=segment.confidence,
                    text_excerpt=segment.text_excerpt,
                    thumbnail_path=segment.thumbnail_path,
                    labels=",".join(leaf_keys),
                    review_status=segment.review_status,
                )
            )
    return remapped


def skip_current_scan() -> None:
    """Request that the currently-running scan title be aborted and left as pending."""
    current = get_current_scan()
    if current is None:
        return
    request_skip_scan(current)


def request_skip_scan(plex_guid: str) -> bool:
    """Request that a specific active scan title be aborted and left as pending."""
    if plex_guid not in _current_guids:
        return False
    _skip_requested_guids.add(plex_guid)
    logger.info("Skip requested for active scan: %s", plex_guid)
    return True

async def _mark_prepare_failed(plex_guid: str, detail: str) -> None:
    await db.upsert_media_scan_stage_status(
        plex_guid,
        "prepare",
        "failed",
        source="scanner",
        detail=detail,
        progress=1.0,
    )


async def scan_video(plex_guid: str, config) -> None:
    _skip_requested_guids.discard(plex_guid)
    await db.clear_scan_cancel_requested(plex_guid)

    job = await db.get_scan_job_by_guid(plex_guid)
    if not job:
        logger.warning("No scan job found for guid %s", plex_guid)
        return

    if bool(job.get("ignored", 0)):
        logger.info("Skipping ignored title: %s", job["title"])
        return

    scan_ratings: set[str] = set(config.scan_ratings) if config.scan_ratings else set()
    if scan_ratings:
        job_rating = (job.get("content_rating") or "").strip()
        if job_rating not in scan_ratings:
            logger.info(
                "Skipping %s: content rating '%s' not in scan_ratings %s",
                job["title"],
                job_rating,
                scan_ratings,
            )
            return

    is_force_scan = bool(job.get("force_scan", 0))
    if not is_force_scan and not config.is_scan_window():
        logger.warning(
            "Attempted to scan outside scan window (not force-scanned): %s. Re-queueing.",
            job["title"],
        )
        await db.update_scan_job_status(plex_guid, "pending")
        await enqueue(plex_guid)
        return

    file_path = str(job["file_path"])
    title = str(job["title"])
    rating_key = str(job.get("rating_key") or "")

    if not os.path.isfile(file_path):
        await _mark_prepare_failed(plex_guid, "File not found")
        logger.error("Video file not found: %s", file_path)
        await db.update_scan_job_status(plex_guid, "failed", error_msg="File not found")
        return

    async with _state_lock:
        _current_guids.add(plex_guid)
    await db.reset_media_scan_state(plex_guid)
    await db.upsert_media_scan_stage_status(
        plex_guid,
        "prepare",
        "running",
        source="scanner",
        detail="Preparing scan job.",
        progress=0.0,
    )
    await db.update_scan_job_status(plex_guid, "scanning", progress=0.0)
    logger.info("Scan started: %s (guid=%s, force_scan=%s)", title, plex_guid, is_force_scan)

    stage_total = len(DETECTOR_STAGE_KEYS) + 2

    try:
        logger.info("Scan prepare started: %s", title)
        if str(getattr(config, "nudenet_model", "320n")).startswith("640") and not str(
            getattr(config, "nudenet_model_path", "")
        ):
            await _ensure_640m_model_async()
        await ensure_semantic_model_async(config)

        await db.upsert_media_scan_stage_status(
            plex_guid,
            "prepare",
            "done",
            source="scanner",
            detail="Scan job prepared.",
            progress=1.0,
        )
        await db.update_scan_job_status(plex_guid, "scanning", progress=1 / stage_total)
        logger.info("Scan prepare complete: %s", title)

        target = MediaScanTarget(
            media_id=plex_guid,
            plex_guid=plex_guid,
            title=title,
            file_path=file_path,
            rating_key=rating_key,
            force_scan=is_force_scan,
        )
        semantic_backend = get_semantic_backend(config)
        image_detectors = {
            "nudity": NudityDetector(
                should_abort=lambda media_id: media_id in _skip_requested_guids,
                should_pause=lambda: _paused,
                should_stop_for_window=lambda: not config.is_scan_window(),
            ),
            "sexual_content": SexualContentDetector(
                backend=semantic_backend,
                should_abort=lambda media_id: media_id in _skip_requested_guids,
                should_pause=lambda: _paused,
                should_stop_for_window=lambda: not config.is_scan_window(),
            ),
            "violence": ViolenceDetector(
                backend=semantic_backend,
                should_abort=lambda media_id: media_id in _skip_requested_guids,
                should_pause=lambda: _paused,
                should_stop_for_window=lambda: not config.is_scan_window(),
            ),
            "drugs": DrugsDetector(
                backend=semantic_backend,
                should_abort=lambda media_id: media_id in _skip_requested_guids,
                should_pause=lambda: _paused,
                should_stop_for_window=lambda: not config.is_scan_window(),
            ),
        }
        detectors = {
            "profanity": ProfanityDetector(),
        }
        segments_inserted = 0
        last_progress = 1 / stage_total
        duration_ms = await get_duration_ms(file_path)
        if not duration_ms:
            raise RuntimeError("Could not determine video duration.")
        step_ms = max(250, int(getattr(config, "scan_step_ms", 250)))
        logger.info(
            "Scan frame sampling started: %s (duration_ms=%d, step_ms=%d)",
            title,
            duration_ms,
            step_ms,
        )
        frames = await sample_video_frames(file_path, step_ms, duration_ms)
        logger.info("Scan frame sampling complete: %s (%d frame(s))", title, len(frames))

        for stage_index, stage_key in enumerate(DETECTOR_STAGE_KEYS, start=1):
            detector = image_detectors.get(stage_key) or detectors.get(stage_key)
            logged_progress_milestone = 0
            logger.info(
                "Scan stage started: %s — %s (%d/%d)",
                title,
                stage_key,
                stage_index,
                len(DETECTOR_STAGE_KEYS),
            )

            await db.upsert_media_scan_stage_status(
                plex_guid,
                stage_key,
                "running",
                category=stage_key,
                source="",
                detail="Detector running.",
                progress=0.0,
            )

            async def report(
                detector_progress: float,
                stage_offset: int = stage_index,
                category_name: str = stage_key,
            ) -> None:
                nonlocal last_progress, logged_progress_milestone
                clamped = min(1.0, max(0.0, detector_progress))
                last_progress = (stage_offset + clamped) / stage_total
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    stage_key,
                    "running",
                    category=stage_key,
                    source="",
                    detail="Detector running.",
                    progress=clamped,
                )
                await db.update_scan_job_status(plex_guid, "scanning", progress=last_progress)
                milestone = int(clamped * 100) // 25 * 25
                if 0 < milestone < 100 and milestone > logged_progress_milestone:
                    logged_progress_milestone = milestone
                    logger.info(
                        "Scan stage progress: %s — %s %d%% (overall %d%%)",
                        title,
                        category_name,
                        milestone,
                        int(last_progress * 100),
                    )

            if stage_key in image_detectors:
                result = await image_detectors[stage_key].scan_frames(
                    target,
                    frames,
                    config,
                    progress_callback=report,
                )
            else:
                result = await detector.scan(target, config, progress_callback=report)

            if result.status == "pending_skip":
                _skip_requested_guids.discard(plex_guid)
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    stage_key,
                    "pending",
                    category=stage_key,
                    source=result.source,
                    detail=result.detail,
                    progress=1.0,
                )
                await db.update_scan_job_status(plex_guid, "pending", progress=last_progress)
                logger.info("Scan stage stopped: %s — %s skipped by user request", title, stage_key)
                return

            if result.status == "pending_pause":
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    stage_key,
                    "pending",
                    category=stage_key,
                    source=result.source,
                    detail=result.detail,
                    progress=1.0,
                )
                await db.update_scan_job_status(plex_guid, "pending", progress=last_progress)
                await enqueue(plex_guid)
                logger.info(
                    "Scan stage stopped: %s — %s paused mid-scan and re-queued",
                    title,
                    stage_key,
                )
                return

            if result.status == "pending_window":
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    stage_key,
                    "pending",
                    category=stage_key,
                    source=result.source,
                    detail=result.detail,
                    progress=1.0,
                )
                await db.update_scan_job_status(plex_guid, "pending", progress=last_progress)
                await enqueue(plex_guid)
                logger.info(
                    "Scan stage stopped: %s — %s scan window ended and title was re-queued",
                    title,
                    stage_key,
                )
                if not _paused:
                    pause_scanner()
                return

            if result.status == "failed":
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    stage_key,
                    "failed",
                    category=stage_key,
                    source=result.source,
                    detail=result.detail,
                    progress=1.0,
                )
                logger.error("Scan stage failed: %s — %s: %s", title, stage_key, result.detail)
                raise RuntimeError(result.detail or f"{result.category} detector failed")

            if result.status == "unavailable":
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    stage_key,
                    "unavailable",
                    category=stage_key,
                    source=result.source,
                    detail=result.detail,
                    progress=1.0,
                )
                last_progress = (stage_index + 1) / stage_total
                await db.update_scan_job_status(plex_guid, "scanning", progress=last_progress)
                logger.info(
                    "Scan stage unavailable: %s — %s (%s)",
                    title,
                    stage_key,
                    result.detail or "detector unavailable",
                )
                continue

            stale_thumbnail_paths: list[str] = []
            cleanup_categories = set(_SCANNER_STAGE_CATEGORY_GROUPS.get(stage_key, set())) | {stage_key}
            for group in cleanup_categories:
                stale_thumbnail_paths.extend(await db.get_thumbnail_paths_for_guid_category(plex_guid, group))
                await db.delete_segments_for_guid_category(plex_guid, group)
            _delete_thumbnail_files(stale_thumbnail_paths)
            remapped_segments = _remap_detector_segments(stage_key, result.segments)
            if remapped_segments:
                await db.insert_segments(
                    plex_guid,
                    [
                        {
                            "media_id": segment.media_id,
                            "title": segment.title,
                            "start_ms": segment.start_ms,
                            "end_ms": segment.end_ms,
                            "category": segment.category,
                            "source": segment.source,
                            "confidence": segment.confidence,
                            "labels": segment.labels,
                            "text_excerpt": segment.text_excerpt,
                            "thumbnail_path": segment.thumbnail_path,
                            "review_status": segment.review_status,
                        }
                        for segment in remapped_segments
                    ],
                )

            counts_by_category: dict[str, int] = {}
            for segment in remapped_segments:
                counts_by_category[segment.category] = counts_by_category.get(segment.category, 0) + 1
            for group in _SCANNER_STAGE_CATEGORY_GROUPS.get(stage_key, set()):
                await db.upsert_media_scan_status(
                    plex_guid,
                    group,
                    "done",
                    source=result.source,
                    detail=result.detail or f"Found {counts_by_category.get(group, 0)} segment(s).",
                    segment_count=counts_by_category.get(group, 0),
                    progress=1.0,
                )

            segment_count = len(remapped_segments)
            segments_inserted += segment_count
            await db.upsert_media_scan_stage_status(
                plex_guid,
                stage_key,
                "done",
                category=stage_key,
                source=result.source,
                detail=result.detail or f"Found {segment_count} segment(s).",
                progress=1.0,
            )
            last_progress = (stage_index + 1) / stage_total
            await db.update_scan_job_status(plex_guid, "scanning", progress=last_progress)
            logger.info(
                "Scan stage complete: %s — %s source=%s segments=%d overall=%d%%",
                title,
                stage_key,
                result.source,
                segment_count,
                int(last_progress * 100),
            )

        logger.info("Scan finalize started: %s", title)
        await db.upsert_media_scan_stage_status(
            plex_guid,
            "finalize",
            "running",
            source="scanner",
            detail="Finalizing scan results.",
            progress=0.0,
        )
        await db.update_scan_job_status(plex_guid, "done", progress=1.0)
        await db.upsert_media_scan_stage_status(
            plex_guid,
            "finalize",
            "done",
            source="scanner",
            detail=f"Scan complete with {segments_inserted} segment(s).",
            progress=1.0,
        )

        if rating_key:
            try:
                client = get_client()
                updater = getattr(client, "update_leapfrog_summary", None)
                if updater is not None:
                    await updater(
                        rating_key=rating_key,
                        status="Scanned",
                        segment_count=segments_inserted,
                    )
            except Exception as exc:
                logger.debug("Could not update summary metadata for %s: %s", plex_guid, exc)

        logger.info("Scan complete: %s — found %d segment(s)", title, segments_inserted)

    except Exception as exc:
        await db.upsert_media_scan_stage_status(
            plex_guid,
            "finalize",
            "failed",
            source="scanner",
            detail=str(exc),
            progress=1.0,
        )
        logger.error("Scan failed for %s: %s", title, exc)
        await db.update_scan_job_status(plex_guid, "failed", error_msg=str(exc))
    finally:
        await db.clear_scan_cancel_requested(plex_guid)
        async with _state_lock:
            _current_guids.discard(plex_guid)


async def _scanner_worker_loop(worker_id: int, get_config_fn) -> None:
    """Single scanner worker — scans persisted queued jobs and respects pause/window."""
    while True:
        config = await get_config_fn()
        in_window = config.is_scan_window()

        job = await db.get_next_ready_queue_job(allow_windowed_only=in_window)
        if job is None:
            await _refresh_queue_size_snapshot()
            if _should_auto_pause_for_scan_window(in_window):
                if not _paused:
                    pause_scanner()
            elif _paused:
                resume_scanner()
            try:
                await asyncio.wait_for(_queue_wakeup_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            _queue_wakeup_event.clear()
            continue

        await _refresh_queue_size_snapshot()
        if _paused:
            resume_scanner()

        plex_guid = str(job["plex_guid"])
        logger.info(
            "Worker %d starting scan of %s (force_scan=%s, in_window=%s)",
            worker_id,
            plex_guid,
            bool(job.get("force_scan", 0)),
            in_window,
        )
        await scan_video(plex_guid, config)

        if bool(job.get("force_scan", 0)):
            await db.set_force_scan(plex_guid, False)
            logger.info("Cleared force_scan for %s", plex_guid)

        await asyncio.sleep(1)


async def scanner_loop(get_config_fn) -> None:
    """Main scanner supervisor — runs multiple scanner workers concurrently."""
    global _worker_pool_size, _restart_requested

    while True:
        config = await get_config_fn()
        worker_count = max(1, int(getattr(config, "scan_workers", 2)))
        _worker_pool_size = worker_count
        _restart_requested = False
        logger.info("Starting scanner pool with %d worker(s)", worker_count)

        worker_tasks = [
            asyncio.create_task(_scanner_worker_loop(index + 1, get_config_fn))
            for index in range(worker_count)
        ]

        try:
            while not _restart_requested:
                done, _ = await asyncio.wait(
                    worker_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=5.0,
                )
                if done:
                    logger.warning("Worker task completed unexpectedly, restarting pool")
                    break

            logger.info("Shutting down worker pool for restart")
            for task in worker_tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*worker_tasks)
            except asyncio.CancelledError:
                pass

        except Exception as exc:
            logger.error("Scanner pool error: %s", exc)
            for task in worker_tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*worker_tasks)
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(5)
