"""Background media scanner orchestration for detector pipelines."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import leapfrog.plex_client as plex_mod

from . import database as db
from .detectors import NudityDetector, ProfanityDetector
from .detectors.nudity import ensure_640m_model_async, ensure_local_640m_model
from .domain import MediaScanTarget
from .logger import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

_scan_queue: asyncio.Queue[str] = asyncio.Queue()
_force_scan_queue: asyncio.Queue[str] = asyncio.Queue()
_paused: bool = False
_current_guids: set[str] = set()
_queued_normal: set[str] = set()
_queued_force: set[str] = set()
_queue_wakeup_event: asyncio.Event = asyncio.Event()
_skip_requested_guids: set[str] = set()
_worker_pool_size: int = 1
_restart_requested: bool = False

# Guards all read-check-modify operations on _current_guids, _queued_normal,
# and _queued_force to prevent duplicate or lost entries under concurrent workers.
_state_lock = asyncio.Lock()


def get_queue_size() -> int:
    return _scan_queue.qsize() + _force_scan_queue.qsize()


def get_worker_pool_size() -> int:
    return _worker_pool_size


async def request_scanner_restart() -> None:
    """Signal scanner to restart worker pool with updated config."""
    global _restart_requested
    _restart_requested = True
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


async def force_scan_job(plex_guid: str) -> None:
    """Prioritize a title for immediate scanning by moving it to the force-scan queue.

    DB flag is set outside the lock; queue state mutations are guarded by _state_lock
    to prevent duplicate entries under concurrent API calls.
    """
    await db.set_force_scan(plex_guid, True)
    async with _state_lock:
        if plex_guid in _current_guids:
            logger.info("Force scan requested for %s, already scanning", plex_guid)
            return
        if plex_guid in _queued_force:
            logger.info("Force scan requested for %s, already at top priority", plex_guid)
            return
        # Remove from normal queue if already there, so it goes to force queue instead.
        if plex_guid in _queued_normal:
            _queued_normal.discard(plex_guid)
            logger.info("Moved %s from normal queue to force queue", plex_guid)
        await _force_scan_queue.put(plex_guid)
        _queued_force.add(plex_guid)
        _queue_wakeup_event.set()
    logger.warning("Force scan activated for %s — will scan immediately", plex_guid)


def is_paused() -> bool:
    return _paused


async def enqueue(plex_guid: str) -> None:
    # Hold _state_lock across the whole check-then-put to prevent two concurrent
    # callers from both passing the guard and inserting duplicates.
    async with _state_lock:
        if plex_guid in _current_guids:
            logger.debug("Skipping enqueue for %s: already scanning", plex_guid)
            return
        if plex_guid in _queued_force or plex_guid in _queued_normal:
            logger.debug("Skipping enqueue for %s: already queued", plex_guid)
            return
        await _scan_queue.put(plex_guid)
        _queued_normal.add(plex_guid)
        _queue_wakeup_event.set()


async def enqueue_pending() -> None:
    """Drain the normal queue and re-enqueue all pending jobs in priority order.

    Safe to call at any time (e.g. on startup or via the reorder-queue API).
    Already-scanning titles (in _current_guids) are not affected.

    Order:
      1. Movies — most recently added first (descending integer rating_key).
      2. TV episodes — grouped by show_guid so all episodes of a show are
         contiguous; shows ordered by their highest rating_key descending
         (most recently added show first); episodes within each show
         ordered by rating_key ascending (episode order).

    Ignored titles are excluded; they remain pending in the DB so they can
    be un-ignored later without a fresh Plex sync.
    """
    # Drain the normal queue and clear tracking set so we can re-enqueue in order.
    # Force-queue and _current_guids are intentionally left untouched.
    async with _state_lock:
        while not _scan_queue.empty():
            try:
                _scan_queue.get_nowait()
            except Exception:
                break
        _queued_normal.clear()

    jobs = await db.get_scan_jobs(status="pending")
    active = [j for j in jobs if not j.get("ignored")]

    movies = [j for j in active if j.get("media_type") != "episode"]
    episodes = [j for j in active if j.get("media_type") == "episode"]

    # Movies: newest Plex item first.
    movies.sort(key=lambda j: int(j.get("rating_key") or 0), reverse=True)

    # TV: group episodes by show_guid; fall back to the show name portion of
    # the title ("ShowName – Season – Episode") when show_guid is absent.
    from collections import defaultdict

    show_buckets: dict[str, list[dict]] = defaultdict(list)
    for ep in episodes:
        key = ep.get("show_guid") or ep.get("title", "").split(" – ")[0]
        show_buckets[key].append(ep)

    # Within each show sort by rating_key asc (episode order).
    for bucket in show_buckets.values():
        bucket.sort(key=lambda j: int(j.get("rating_key") or 0))

    # Order shows by the highest rating_key in each bucket desc
    # (most recently added show comes first).
    ordered_shows = sorted(
        show_buckets.values(),
        key=lambda eps: max(int(j.get("rating_key") or 0) for j in eps),
        reverse=True,
    )

    ordered = movies + [ep for eps in ordered_shows for ep in eps]

    enqueued = 0
    for job in ordered:
        await enqueue(job["plex_guid"])
        enqueued += 1
    if enqueued:
        logger.info(
            "Queued %d pending scan jobs (%d movies, %d episodes, %d ignored skipped)",
            enqueued,
            len(movies),
            len(episodes),
            len(jobs) - len(active),
        )


def _ensure_local_640m_model() -> str:
    """Backward-compatible wrapper for settings validation."""
    return ensure_local_640m_model()


async def _ensure_640m_model_async() -> str:
    """Backward-compatible wrapper for scanner code paths."""
    return await ensure_640m_model_async()


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


async def scan_video(plex_guid: str, config) -> None:
    _skip_requested_guids.discard(plex_guid)

    job = await db.get_scan_job_by_guid(plex_guid)
    if not job:
        logger.warning("No scan job found for guid %s", plex_guid)
        return

    # Check if title is marked as ignored
    is_ignored = bool(job.get("ignored", 0))
    if is_ignored:
        logger.info("Skipping ignored title: %s", job["title"])
        return

    # Enforce scan_ratings: skip titles whose content rating is not in the configured list.
    # This check must live here (not just in the watcher) because enqueue_pending() re-queues
    # all pending jobs on startup without re-checking ratings.
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

    # Safety check: Don't scan outside window unless force-scanned
    is_force_scan = bool(job.get("force_scan", 0))
    if not is_force_scan and not config.is_scan_window():
        logger.warning("Attempted to scan outside scan window (not force-scanned): %s. Re-queueing.", job["title"])
        await db.update_scan_job_status(plex_guid, "pending")
        await enqueue(plex_guid)
        return

    file_path = job["file_path"]
    title = job["title"]
    rating_key = str(job.get("rating_key") or "")

    if not os.path.isfile(file_path):
        logger.error("Video file not found: %s", file_path)
        await db.update_scan_job_status(plex_guid, "failed", error_msg="File not found")
        return

    async with _state_lock:
        _current_guids.add(plex_guid)
    await db.seed_media_scan_statuses(plex_guid)
    await db.update_scan_job_status(plex_guid, "scanning", progress=0.0)
    logger.info("Scanning: %s", title)

    try:
        if str(getattr(config, "nudenet_model", "320n")).startswith("640") and not str(
            getattr(config, "nudenet_model_path", "")
        ):
            await _ensure_640m_model_async()

        target = MediaScanTarget(
            media_id=plex_guid,
            plex_guid=plex_guid,
            title=title,
            file_path=file_path,
            rating_key=rating_key,
            force_scan=is_force_scan,
        )
        detectors = [
            NudityDetector(
                should_abort=lambda media_id: media_id in _skip_requested_guids,
                should_pause=lambda: _paused,
                should_stop_for_window=lambda: not config.is_scan_window(),
            ),
            ProfanityDetector(),
        ]
        segments_inserted = 0
        last_progress = 0.0

        for index, detector in enumerate(detectors):
            await db.upsert_media_scan_status(
                plex_guid,
                detector.category,
                "scanning",
                source="",
                detail="Detector running.",
            )

            async def report(detector_progress: float, detector_index: int = index) -> None:
                nonlocal last_progress
                last_progress = (detector_index + detector_progress) / len(detectors)
                await db.update_scan_job_status(plex_guid, "scanning", progress=last_progress)

            result = await detector.scan(target, config, progress_callback=report)

            if result.status == "pending_skip":
                _skip_requested_guids.discard(plex_guid)
                await db.upsert_media_scan_status(
                    plex_guid,
                    result.category,
                    "pending",
                    source=result.source,
                    detail=result.detail,
                )
                await db.update_scan_job_status(plex_guid, "pending", progress=last_progress)
                logger.info("Scan of '%s' skipped by user request", title)
                return

            if result.status == "pending_pause":
                await db.upsert_media_scan_status(
                    plex_guid,
                    result.category,
                    "pending",
                    source=result.source,
                    detail=result.detail,
                )
                await db.update_scan_job_status(plex_guid, "pending", progress=last_progress)
                await enqueue(plex_guid)
                logger.info("Scan paused mid-way through %s, re-queued", title)
                return

            if result.status == "pending_window":
                await db.upsert_media_scan_status(
                    plex_guid,
                    result.category,
                    "pending",
                    source=result.source,
                    detail=result.detail,
                )
                await db.update_scan_job_status(plex_guid, "pending", progress=last_progress)
                await enqueue(plex_guid)
                logger.info("Scan window ended during scan of '%s', re-queued", title)
                if not _paused:
                    pause_scanner()
                return

            if result.status == "failed":
                await db.upsert_media_scan_status(
                    plex_guid,
                    result.category,
                    "failed",
                    source=result.source,
                    detail=result.detail,
                )
                raise RuntimeError(result.detail or f"{result.category} detector failed")

            if result.status == "unavailable":
                await db.upsert_media_scan_status(
                    plex_guid,
                    result.category,
                    "unavailable",
                    source=result.source,
                    detail=result.detail,
                )
                continue

            await db.delete_segments_for_guid_category(plex_guid, result.category)
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
            segments_inserted += len(result.segments)
            await db.upsert_media_scan_status(
                plex_guid,
                result.category,
                "done",
                source=result.source,
                detail=result.detail or f"Found {len(result.segments)} segment(s).",
            )

        await db.update_scan_job_status(plex_guid, "done", progress=1.0)

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
        logger.error("Scan failed for %s: %s", title, exc)
        await db.update_scan_job_status(plex_guid, "failed", error_msg=str(exc))
    finally:
        async with _state_lock:
            _current_guids.discard(plex_guid)


async def _scanner_worker_loop(worker_id: int, get_config_fn) -> None:
    """Single scanner worker — scans queued jobs and respects pause/scan window."""
    while True:
        config = await get_config_fn()

        try:
            # Prioritize explicit force-scan requests ahead of normal queue items.
            try:
                plex_guid = _force_scan_queue.get_nowait()
                async with _state_lock:
                    _queued_force.discard(plex_guid)
            except asyncio.QueueEmpty:
                plex_guid = await asyncio.wait_for(_scan_queue.get(), timeout=30)
                async with _state_lock:
                    _queued_normal.discard(plex_guid)
        except asyncio.TimeoutError:
            # Queue is empty, respect scan window
            if not config.is_scan_window():
                if not _paused:
                    pause_scanner()
            else:
                if _paused:
                    resume_scanner()
            try:
                await asyncio.wait_for(_queue_wakeup_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            _queue_wakeup_event.clear()
            continue

        job = await db.get_scan_job_by_guid(plex_guid)
        if not job or job["status"] not in ("pending", "scanning"):
            logger.debug(f"Skipping {plex_guid}: not found or wrong status")
            continue

        # Check if this job should run: is_force_scan OR within scan window
        is_force_scan = bool(job.get("force_scan", 0))
        in_window = config.is_scan_window()

        if not is_force_scan and not in_window:
            # Outside window and not force-scan, re-queue and wait
            logger.debug(f"Job {plex_guid} outside scan window and not force-scan, re-queuing")
            await enqueue(plex_guid)
            if not _paused:
                pause_scanner()
            # If a force-scan job arrived, don't sleep; process it immediately.
            if not _force_scan_queue.empty():
                continue
            try:
                await asyncio.wait_for(_queue_wakeup_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            _queue_wakeup_event.clear()
            continue

        if _paused:
            resume_scanner()

        logger.info(
            "Worker %d starting scan of %s (force_scan=%s, in_window=%s)",
            worker_id,
            plex_guid,
            is_force_scan,
            in_window,
        )
        await scan_video(plex_guid, config)
        
        # Clear force_scan flag after job completes
        if is_force_scan:
            await db.set_force_scan(plex_guid, False)
            logger.info(f"Cleared force_scan for {plex_guid}")

        await asyncio.sleep(1)  # Brief pause between scans


async def scanner_loop(get_config_fn) -> None:
    """Main scanner supervisor — runs multiple scanner workers concurrently."""
    global _worker_pool_size, _restart_requested

    while True:
        # Re-apply queue ordering on every start/restart so settings changes
        # (or an explicit reorder-queue API call) take effect immediately.
        await enqueue_pending()

        config = await get_config_fn()
        worker_count = max(1, int(getattr(config, "scan_workers", 2)))
        _worker_pool_size = worker_count
        _restart_requested = False
        logger.info("Starting scanner pool with %d worker(s)", worker_count)

        # Create worker tasks
        worker_tasks = [
            asyncio.create_task(_scanner_worker_loop(i + 1, get_config_fn))
            for i in range(worker_count)
        ]

        try:
            # Run workers until restart or unexpected completion.
            # timeout=5 ensures _restart_requested is polled promptly even while
            # all workers are mid-scan (workers never complete normally, so without
            # a timeout asyncio.wait would block indefinitely).
            while not _restart_requested:
                done, _ = await asyncio.wait(
                    worker_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=5.0,
                )
                if done:
                    # A worker unexpectedly completed; restart the pool
                    logger.warning("Worker task completed unexpectedly, restarting pool")
                    break

            # Restart requested or worker failed; cancel all tasks
            logger.info("Shutting down worker pool for restart")
            for task in worker_tasks:
                if not task.done():
                    task.cancel()
            # Wait for all tasks to complete/cancel
            try:
                await asyncio.gather(*worker_tasks)
            except asyncio.CancelledError:
                pass
            # Loop continues, which will pick up config changes and restart

        except Exception as exc:
            logger.error("Scanner pool error: %s", exc)
            for task in worker_tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*worker_tasks)
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(5)  # Backoff before retry
