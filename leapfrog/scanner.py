"""Background media scanner orchestration for detector pipelines."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import leapfrog.plex_client as plex_mod

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
from .domain import MediaScanTarget, SUPPORTED_CATEGORIES
from .frame_extractor import get_duration_ms, sample_video_frames
from .logger import get_logger

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
    _paused = False
    logger.info("Scanner resumed")


def is_paused() -> bool:
    return _paused


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
    logger.info("Scanning: %s", title)

    stage_total = len(SUPPORTED_CATEGORIES) + 2

    try:
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
        frames = await sample_video_frames(file_path, step_ms, duration_ms)

        for stage_index, category in enumerate(SUPPORTED_CATEGORIES, start=1):
            detector = image_detectors.get(category) or detectors.get(category)

            await db.upsert_media_scan_stage_status(
                plex_guid,
                category,
                "running",
                category=category,
                source="",
                detail="Detector running.",
                progress=0.0,
            )
            await db.upsert_media_scan_status(
                plex_guid,
                category,
                "running",
                source="",
                detail="Detector running.",
                segment_count=0,
                progress=0.0,
            )

            async def report(detector_progress: float, stage_offset: int = stage_index) -> None:
                nonlocal last_progress
                clamped = min(1.0, max(0.0, detector_progress))
                last_progress = (stage_offset + clamped) / stage_total
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    category,
                    "running",
                    category=category,
                    source="",
                    detail="Detector running.",
                    progress=clamped,
                )
                await db.upsert_media_scan_status(
                    plex_guid,
                    category,
                    "running",
                    source="",
                    detail="Detector running.",
                    segment_count=0,
                    progress=clamped,
                )
                await db.update_scan_job_status(plex_guid, "scanning", progress=last_progress)

            if category in image_detectors:
                result = await image_detectors[category].scan_frames(
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
                    category,
                    "pending",
                    category=category,
                    source=result.source,
                    detail=result.detail,
                    progress=1.0,
                )
                await db.upsert_media_scan_status(
                    plex_guid,
                    category,
                    "pending",
                    source=result.source,
                    detail=result.detail,
                    segment_count=0,
                    progress=1.0,
                )
                await db.update_scan_job_status(plex_guid, "pending", progress=last_progress)
                logger.info("Scan of '%s' skipped by user request", title)
                return

            if result.status == "pending_pause":
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    category,
                    "pending",
                    category=category,
                    source=result.source,
                    detail=result.detail,
                    progress=1.0,
                )
                await db.upsert_media_scan_status(
                    plex_guid,
                    category,
                    "pending",
                    source=result.source,
                    detail=result.detail,
                    segment_count=0,
                    progress=1.0,
                )
                await db.update_scan_job_status(plex_guid, "pending", progress=last_progress)
                await enqueue(plex_guid)
                logger.info("Scan paused mid-way through %s, re-queued", title)
                return

            if result.status == "pending_window":
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    category,
                    "pending",
                    category=category,
                    source=result.source,
                    detail=result.detail,
                    progress=1.0,
                )
                await db.upsert_media_scan_status(
                    plex_guid,
                    category,
                    "pending",
                    source=result.source,
                    detail=result.detail,
                    segment_count=0,
                    progress=1.0,
                )
                await db.update_scan_job_status(plex_guid, "pending", progress=last_progress)
                await enqueue(plex_guid)
                logger.info("Scan window ended during scan of '%s', re-queued", title)
                if not _paused:
                    pause_scanner()
                return

            if result.status == "failed":
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    category,
                    "failed",
                    category=category,
                    source=result.source,
                    detail=result.detail,
                    progress=1.0,
                )
                await db.upsert_media_scan_status(
                    plex_guid,
                    category,
                    "failed",
                    source=result.source,
                    detail=result.detail,
                    segment_count=0,
                    progress=1.0,
                )
                raise RuntimeError(result.detail or f"{result.category} detector failed")

            if result.status == "unavailable":
                await db.upsert_media_scan_stage_status(
                    plex_guid,
                    category,
                    "unavailable",
                    category=category,
                    source=result.source,
                    detail=result.detail,
                    progress=1.0,
                )
                await db.upsert_media_scan_status(
                    plex_guid,
                    category,
                    "unavailable",
                    source=result.source,
                    detail=result.detail,
                    segment_count=0,
                    progress=1.0,
                )
                last_progress = (stage_index + 1) / stage_total
                await db.update_scan_job_status(plex_guid, "scanning", progress=last_progress)
                continue

            stale_thumbnail_paths = await db.get_thumbnail_paths_for_guid_category(
                plex_guid,
                result.category,
            )
            await db.delete_segments_for_guid_category(plex_guid, result.category)
            _delete_thumbnail_files(stale_thumbnail_paths)
            if result.segments:
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
                        for segment in result.segments
                    ],
                )

            segment_count = len(result.segments)
            segments_inserted += segment_count
            await db.upsert_media_scan_stage_status(
                plex_guid,
                category,
                "done",
                category=category,
                source=result.source,
                detail=result.detail or f"Found {segment_count} segment(s).",
                progress=1.0,
            )
            await db.upsert_media_scan_status(
                plex_guid,
                category,
                "done",
                source=result.source,
                detail=result.detail or f"Found {segment_count} segment(s).",
                segment_count=segment_count,
                progress=1.0,
            )
            last_progress = (stage_index + 1) / stage_total
            await db.update_scan_job_status(plex_guid, "scanning", progress=last_progress)

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
                client = plex_mod.get_client()
                await client.update_leapfrog_summary(
                    rating_key=rating_key,
                    status="Scanned",
                    segment_count=segments_inserted,
                )
            except Exception as exc:
                logger.debug("Could not update Plex summary metadata for %s: %s", plex_guid, exc)

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
            if not in_window:
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
