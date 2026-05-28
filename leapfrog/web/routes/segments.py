import asyncio
import json
import mimetypes
import os
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ...logger import get_logger
from ...preferences import (
    get_effective_skip_segments,
    get_preference_threshold_settings,
    get_resolved_preferences_for_user,
)
from ...segment_export import build_canonical_media_export, build_sidecar_payload
from ...adapters import get_segment_adapter, list_segment_adapters
import leapfrog.plex_client as plex_mod
from ... import database as db
from ...domain import SUPPORTED_CATEGORIES

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["segments"])

# Pending debounce tasks keyed by plex_guid — cancelled and replaced on each delete.
# This ensures bulk deletes trigger at most one Plex metadata write per title.
_pending_summary_tasks: dict[str, asyncio.Task] = {}

# scan_labels changes only when the user edits Settings, so a 30-second TTL avoids
# opening a second aiosqlite connection on every segment-panel expand.
_scan_labels_cache: tuple[float, str] | None = None  # (monotonic_time, raw_json_str)
_SCAN_LABELS_CACHE_TTL = 30.0


class ClearSegmentsRequest(BaseModel):
    """Request body for clearing stored segment results."""

    reset_scan_state: bool = True
    clear_queue: bool = True


def _invalidate_scan_labels_cache() -> None:
    global _scan_labels_cache
    _scan_labels_cache = None


async def _do_refresh_summary(plex_guid: str) -> None:
    """Perform the actual Plex summary metadata update after a short debounce delay."""
    await asyncio.sleep(2)  # coalesce rapid sequential deletes
    _pending_summary_tasks.pop(plex_guid, None)

    try:
        job = await db.get_scan_job_by_guid(plex_guid)
        if not job:
            return

        rating_key = str(job.get("rating_key") or "")
        if not rating_key:
            return

        # COUNT query — avoids loading all segment rows just to get the total.
        segment_count = await db.count_segments_for_guid(plex_guid)
        status = "Scanned" if job.get("status") == "done" else "Pending"

        client = plex_mod.get_client()
        await client.update_leapfrog_summary(
            rating_key=rating_key,
            status=status,
            segment_count=segment_count,
        )
    except Exception as exc:
        logger.debug("Could not refresh Plex summary for guid=%s: %s", plex_guid, exc)


def _refresh_leapfrog_summary_for_guid(plex_guid: str) -> None:
    """Schedule a debounced Plex summary refresh.

    Any pending refresh for the same guid is cancelled before scheduling a new
    one, so bulk deletes of N segments produce at most one metadata write.
    """
    existing = _pending_summary_tasks.pop(plex_guid, None)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_do_refresh_summary(plex_guid))
    _pending_summary_tasks[plex_guid] = task


def _plex_image_proxy_url(path: str) -> str:
    if not path:
        return ""
    return f"/api/plex-image?path={quote(path, safe='')}"


def _delete_thumbnail_files(paths: list[str]) -> None:
    """Best-effort thumbnail cleanup for removed segment rows."""
    for path in {str(value).strip() for value in paths if str(value).strip()}:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("Could not delete thumbnail %s: %s", path, exc)


def _summarize_scan_state(
    status_rows: list[dict],
    *,
    job: dict | None = None,
    segment_count: int = 0,
) -> str:
    if not status_rows:
        if job and job.get("queue_state") == "queued":
            return "queued"
        return "unscanned"
    statuses = {str(row.get("status") or "") for row in status_rows}
    if "failed" in statuses:
        return "failed"
    terminal = {"done", "unavailable", "canceled"}
    if any(status in terminal for status in statuses) and any(status not in terminal for status in statuses):
        return "partially_scanned"
    if job and job.get("status") == "scanning":
        return "scanning"
    if "running" in statuses:
        return "scanning"
    if statuses <= {"pending"}:
        if job and job.get("queue_state") == "queued":
            return "queued"
        return "unscanned"
    if any(status not in terminal for status in statuses):
        return "partially_scanned"
    return "scanned_flagged" if segment_count > 0 else "scanned_clean"


# ── Libraries / titles tree ───────────────────────────────────────────────────

@router.get("/libraries")
async def get_libraries():
    """Return all Plex library sections."""
    try:
        client = plex_mod.get_client()
        sections = await client.get_library_sections()
        return {
            "libraries": [
                {"id": s.section_id, "title": s.title, "type": s.section_type}
                for s in sections
            ]
        }
    except RuntimeError:
        return {"libraries": [], "error": "Plex not configured"}


@router.post("/libraries/{library_id}/sync")
async def sync_library(library_id: str):
    """Sync a library from Plex into DB. Returns count of new titles added."""
    excluded = set(json.loads(await db.get_setting("excluded_library_ids", "[]")))
    if library_id in excluded:
        return {"ok": False, "error": "Library is excluded from scanning"}
    try:
        client = plex_mod.get_client()
        items = await client.get_library_items(library_id)
        logger.info(f"Syncing library {library_id}: found {len(items)} items from Plex")
        
        scan_ratings = set(json.loads(await db.get_setting("scan_ratings", "[]")))
        file_items = [i for i in items if i.file_path]
        existing_guids = await db.get_existing_guids([i.plex_guid for i in file_items])

        # Refresh mutable Plex metadata for all existing titles in one transaction
        # so that manual rating changes in Plex are reflected after sync.
        await db.refresh_scan_job_metadata_batch([
            (i.plex_guid, i.title, i.file_path, i.rating_key, i.content_rating, i.year)
            for i in file_items if i.plex_guid in existing_guids
        ])

        # Remove DB entries for titles Plex no longer reports in this library
        # (e.g. deleted files or removed duplicates).
        plex_guids = [i.plex_guid for i in file_items]
        removed = await db.delete_scan_jobs_not_in(library_id, plex_guids)
        if removed:
            logger.info(f"Library {library_id} sync: removed {removed} stale titles")

        added = 0
        for item in file_items:
            if item.plex_guid in existing_guids:
                continue
            if scan_ratings:
                # Filter strictly: "" = unrated, only included when the
                # Unrated checkbox is ticked (saves "" in scan_ratings).
                if (item.content_rating or "") not in scan_ratings:
                    continue
                await db.upsert_scan_job(
                    plex_guid=item.plex_guid,
                    title=item.title,
                    file_path=item.file_path,
                    rating_key=item.rating_key,
                library_id=item.library_id,
                library_title=item.library_title,
                    content_rating=item.content_rating,
                    media_type=item.media_type,
                    year=item.year,
                    show_guid=getattr(item, "show_guid", ""),
                )
            added += 1

        logger.info(f"Library {library_id} synced: {added} new titles added")
        return {"ok": True, "synced": len(items), "new": added, "removed": removed}
    except RuntimeError as e:
        logger.error(f"Plex client error during library sync: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/libraries/{library_id}/titles")
async def get_titles_in_library(library_id: str):
    """Return all scan jobs (titles) for a given library, with Plex poster URLs."""
    jobs = await db.get_scan_jobs_by_library(library_id)
    seg_counts = await db.get_segment_counts_for_library(library_id)
    seg_counts_by_category = await db.get_segment_counts_by_category_for_library(library_id)
    scan_statuses = await db.get_media_scan_statuses([job["plex_guid"] for job in jobs])

    # Apply scan_ratings filter to the library view so it matches what the
    # scanner will actually process.
    scan_ratings_raw = json.loads(await db.get_setting("scan_ratings", "[]"))
    scan_ratings: set[str] = set(scan_ratings_raw)
    if scan_ratings:
        # Filter strictly by the ratings the user configured.
        # "" (empty) = Plex left the title unrated; it only shows when the
        # "Unrated" checkbox is ticked, which stores "" in scan_ratings.
        jobs = [j for j in jobs if (j.get("content_rating") or "") in scan_ratings]
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        client = None

    result = []
    # Cache resolved show metadata by show name to avoid repeated Plex API calls.
    # Value: (show_guid, show_title, show_poster_url, show_rating_key, season_rating_key)
    show_meta_by_name: dict[str, tuple[str, str, str, str, str]] = {}
    for job in jobs:
        thumb_url = ""
        poster_url = ""
        show_guid = ""
        show_title = ""
        show_rating_key = ""
        season_rating_key = ""
        if client and job.get("rating_key"):
            rating_key = job["rating_key"]
            thumb_url = _plex_image_proxy_url(f"/library/metadata/{rating_key}/thumb")
            if job.get("media_type") == "episode":
                # Resolve true show-level poster path from episode metadata.
                # Example resolved path from Plex: /library/metadata/<showKey>/thumb/<version>
                # which matches how Plex itself loads show posters.
                parsed_show_name = (job.get("title", "").split(" \u2013 ")[0] or "").strip()
                if parsed_show_name and parsed_show_name in show_meta_by_name:
                    show_guid, show_title, poster_url, show_rating_key, season_rating_key = show_meta_by_name[parsed_show_name]
                else:
                    resolved_show_guid, resolved_show_title, show_thumb_path, resolved_show_rk, resolved_season_rk = await client.get_episode_show_art(rating_key)
                    show_guid = resolved_show_guid
                    show_title = resolved_show_title or parsed_show_name
                    poster_url = _plex_image_proxy_url(show_thumb_path) if show_thumb_path else ""
                    show_rating_key = resolved_show_rk
                    season_rating_key = resolved_season_rk
                    if parsed_show_name:
                        show_meta_by_name[parsed_show_name] = (show_guid, show_title, poster_url, show_rating_key, season_rating_key)
            else:
                poster_url = thumb_url
        result.append({
            "plex_guid": job["plex_guid"],
            "rating_key": job.get("rating_key", ""),
            "title": job["title"],
            "status": job["status"],
            "progress": job["progress"],
            "queue_state": job.get("queue_state", "idle"),
            "queue_position": job.get("queue_position"),
            "queue_priority": job.get("queue_priority", 0),
            "cancel_requested": bool(job.get("cancel_requested", 0)),
            "finished_at": job.get("finished_at"),
            "thumb_url": thumb_url,
            "poster_url": poster_url,
            "show_guid": show_guid,
            "show_title": show_title,
            "show_rating_key": show_rating_key,
            "season_rating_key": season_rating_key,
            "segment_count": seg_counts.get(job["plex_guid"], 0),
            "segment_counts_by_category": seg_counts_by_category.get(job["plex_guid"], {}),
            "content_rating": job.get("content_rating", ""),
            "media_type": job.get("media_type", "movie"),
            "year": job.get("year"),
            "ignored": bool(job.get("ignored", 0)),
            "scan_statuses": {
                row["category"]: {
                    "status": row["status"],
                    "source": row["source"],
                    "detail": row["detail"],
                    "segment_count": row.get("segment_count", 0),
                    "progress": row.get("progress", 0),
                    "updated_at": row["updated_at"],
                }
                for row in scan_statuses.get(job["plex_guid"], [])
            },
            "analysis_state": _summarize_scan_state(
                scan_statuses.get(job["plex_guid"], []),
                job=job,
                segment_count=seg_counts.get(job["plex_guid"], 0),
            ),
        })
    return {"titles": result}


@router.get("/plex-image")
async def get_plex_image(path: str):
    """Proxy Plex images through Leapfrog so artwork loads from remote clients."""
    if not path:
        raise HTTPException(status_code=400, detail="Missing image path")

    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    content, content_type = await client.fetch_image(path)
    if not content:
        raise HTTPException(status_code=404, detail="Image not available")

    return Response(content=content, media_type=content_type or "image/jpeg")


@router.get("/titles/{plex_guid:path}/segments")
async def get_segments_for_title(
    plex_guid: str,
    user: str | None = None,
    category: list[str] | None = Query(default=None),
):
    """Return all segments for a specific title with all detected labels."""
    global _scan_labels_cache
    now = time.monotonic()
    if _scan_labels_cache and now - _scan_labels_cache[0] < _SCAN_LABELS_CACHE_TTL:
        # Cache hit: fetch segments only — single DB connection open.
        segments = await db.get_segments_for_guid(plex_guid)
        scan_labels_raw_str = _scan_labels_cache[1]
    else:
        # Cache miss: fetch segments + setting together in one connection open.
        segments, scan_labels_raw_str = await db.get_segments_for_guid_with_setting(
            plex_guid, "scan_labels", "[]"
        )
        _scan_labels_cache = (now, scan_labels_raw_str)
    scan_labels_raw = json.loads(scan_labels_raw_str)
    enabled_labels = set(scan_labels_raw) if isinstance(scan_labels_raw, list) else set()
    category_filter = {value.strip() for value in category or [] if value.strip()}
    if category_filter:
        segments = [
            segment
            for segment in segments
            if str(segment.get("category") or "") in category_filter
        ]
    effective_segment_ids: set[int] = set()
    if user:
        threshold_defaults = await get_preference_threshold_settings()
        preferences = await get_resolved_preferences_for_user(
            user,
            threshold_defaults=threshold_defaults,
        )
        effective_segment_ids = {
            int(seg["id"])
            for seg in get_effective_skip_segments(segments, preferences)
            if seg.get("id") is not None
        }
    result = []
    for seg in segments:
        labels = seg.get("labels", "") or ""
        if enabled_labels and labels and str(seg.get("category") or "sex_nudity_immodesty") == "sex_nudity_immodesty":
            filtered = [l.strip() for l in labels.split(",") if l.strip() in enabled_labels]
            labels = ",".join(filtered)
        result.append({
            "id": seg["id"],
            "plex_guid": seg["plex_guid"],
            "media_id": seg["media_id"],
            "title": seg["title"],
            "start_ms": seg["start_ms"],
            "end_ms": seg["end_ms"],
            "start_time": seg["start_time"],
            "end_time": seg["end_time"],
            "category": seg["category"],
            "source": seg["source"],
            "confidence": seg["confidence"],
            "has_thumbnail": bool(seg.get("thumbnail_path")),
            "thumbnail_url": f"/api/thumbnails/{seg['id']}" if seg.get("thumbnail_path") else "",
            "created_at": seg["created_at"],
            "updated_at": seg.get("updated_at"),
            "labels": labels,
            "text_excerpt": seg.get("text_excerpt"),
            "review_status": seg.get("review_status"),
            "would_skip": int(seg["id"]) in effective_segment_ids if user else None,
        })
    return {"segments": result}


async def _build_scan_detail_payload(plex_guid: str, user: str | None = None) -> dict:
    job = await db.get_scan_job_by_guid(plex_guid)
    segments = await db.get_segments_for_guid(plex_guid)
    if not job and not segments:
        raise HTTPException(status_code=404, detail="Title not found")

    status_rows = await db.get_media_scan_statuses_for_media(plex_guid)
    stage_rows = await db.get_media_scan_stage_statuses_for_media(plex_guid)
    counts_by_category = {
        category: 0 for category in SUPPORTED_CATEGORIES
    }
    for row in status_rows:
        category_key = str(row.get("category") or "")
        if category_key in counts_by_category:
            counts_by_category[category_key] = int(row.get("segment_count") or 0)
    if not any(counts_by_category.values()):
        for segment in segments:
            category_key = str(segment.get("category") or "nudity")
            counts_by_category[category_key] = counts_by_category.get(category_key, 0) + 1

    preference_resolution_success: bool | None = None
    effective_segment_count: int | None = None
    if user:
        try:
            threshold_defaults = await get_preference_threshold_settings()
            preferences = await get_resolved_preferences_for_user(
                user,
                threshold_defaults=threshold_defaults,
            )
            preference_resolution_success = True
            effective_segment_count = len(get_effective_skip_segments(segments, preferences))
        except Exception:
            preference_resolution_success = False
            effective_segment_count = None

    queued_snapshot = await db.get_queue_snapshot()
    queued_entry = next((row for row in queued_snapshot if row["plex_guid"] == plex_guid), None)
    total_segment_count = sum(counts_by_category.values())

    return {
        "media_id": plex_guid,
        "title": (job or {}).get("title") or (segments[0].get("title") if segments else "") or "",
        "analysis_state": _summarize_scan_state(
            status_rows,
            job=job,
            segment_count=total_segment_count,
        ),
        "segment_count": total_segment_count,
        "segment_counts_by_category": counts_by_category,
        "scan_statuses": status_rows,
        "stage_statuses": stage_rows,
        "last_scan_time": (job or {}).get("finished_at"),
        "export_available": bool(job or segments),
        "preference_resolution_success": preference_resolution_success,
        "effective_segment_count": effective_segment_count,
        "queue": {
            "state": (queued_entry or job or {}).get("queue_state", "idle"),
            "position": (queued_entry or job or {}).get("queue_position"),
            "priority": (queued_entry or job or {}).get("queue_priority", 0),
            "cancel_requested": bool((queued_entry or job or {}).get("cancel_requested", 0)),
        },
        "job_status": (job or {}).get("status", "unknown"),
    }


@router.get("/titles/{plex_guid:path}/export")
async def export_segments_for_title(
    plex_guid: str,
    user: str | None = None,
    adapter: str | None = None,
    category: list[str] | None = Query(default=None),
    min_confidence: float | None = None,
):
    """Return a canonical or adapter-shaped export payload for one title."""
    categories = {value.strip() for value in category or [] if value.strip()}
    try:
        media_export = await build_canonical_media_export(
            plex_guid,
            user_id=user,
            categories=categories or None,
            min_confidence=min_confidence,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Title not found")

    if not adapter:
        return media_export.to_record()

    if adapter not in list_segment_adapters():
        raise HTTPException(status_code=400, detail="Unknown adapter")
    return get_segment_adapter(adapter).adapt(media_export)


@router.get("/titles/{plex_guid:path}/sidecar")
async def get_sidecar_for_title(
    plex_guid: str,
    user: str | None = None,
    category: list[str] | None = Query(default=None),
    min_confidence: float | None = None,
):
    """Return the portable sidecar JSON payload for one title."""
    categories = {value.strip() for value in category or [] if value.strip()}
    try:
        return await build_sidecar_payload(
            plex_guid,
            user_id=user,
            categories=categories or None,
            min_confidence=min_confidence,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Title not found")


@router.get("/titles/{plex_guid:path}/scan-status")
async def get_scan_status_for_title(plex_guid: str, user: str | None = None):
    """Return per-category scan status and segment counts for one title."""
    payload = await _build_scan_detail_payload(plex_guid, user)
    return {
        "media_id": payload["media_id"],
        "title": payload["title"],
        "analysis_state": payload["analysis_state"],
        "segment_counts_by_category": payload["segment_counts_by_category"],
        "scan_statuses": payload["scan_statuses"],
        "last_scan_time": payload["last_scan_time"],
        "export_available": payload["export_available"],
        "preference_resolution_success": payload["preference_resolution_success"],
        "effective_segment_count": payload["effective_segment_count"],
        "queue": payload["queue"],
        "job_status": payload["job_status"],
    }


@router.get("/titles/{plex_guid:path}/scan-details")
async def get_scan_details_for_title(plex_guid: str, user: str | None = None):
    """Return rich per-title scan detail including per-stage status."""
    return await _build_scan_detail_payload(plex_guid, user)


@router.get("/titles/{plex_guid:path}/scan-timeline")
async def get_scan_timeline_for_title(plex_guid: str):
    """Return the ordered stage timeline for a title scan."""
    detail = await _build_scan_detail_payload(plex_guid, None)
    return {
        "media_id": detail["media_id"],
        "title": detail["title"],
        "timeline": detail["stage_statuses"],
    }


@router.post("/segments/{segment_id}/jump")
async def jump_to_segment(segment_id: int):
    """Seek an active, controllable Plex session for this title to the segment start."""
    seg = await db.get_segment_by_id(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    job = await db.get_scan_job_by_guid(seg["plex_guid"])

    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    sessions = await client.get_active_sessions()

    target = None
    rating_key = str(job.get("rating_key", "")) if job else ""

    if rating_key:
        target = next(
            (s for s in sessions if s.is_controllable and str(s.rating_key) == rating_key),
            None,
        )

    if target is None:
        target = next(
            (s for s in sessions if s.is_controllable and s.plex_guid == seg["plex_guid"]),
            None,
        )

    if target is None:
        raise HTTPException(
            status_code=409,
            detail="No active controllable Plex playback found for this title. Start the title in Plex first.",
        )

    ok = await client.seek(
        target.client_identifier,
        int(seg["start_ms"]),
        target.client_address,
        target.client_port,
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to seek Plex client")

    return {
        "ok": True,
        "segment_id": segment_id,
        "seek_to_ms": int(seg["start_ms"]),
        "client": target.client_title,
        "user": target.user,
    }


@router.get("/segments/{segment_id}/stream")
async def stream_segment_source(segment_id: int):
    """Stream the source media file for a segment so the web UI can preview it directly."""
    seg = await db.get_segment_by_id(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    job = await db.get_scan_job_by_guid(seg["plex_guid"])
    if not job or not job.get("file_path"):
        raise HTTPException(status_code=404, detail="Source file not found for this segment")

    file_path = job["file_path"]
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Source media file does not exist on disk")

    media_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    return FileResponse(path=file_path, media_type=media_type)


@router.delete("/segments/{segment_id}")
async def delete_segment(segment_id: int):
    seg = await db.get_segment_by_id(segment_id)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    thumbnail_path = str(seg.get("thumbnail_path") or "").strip()
    deleted = await db.delete_segment(segment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Segment not found")

    _delete_thumbnail_files([thumbnail_path])
    _refresh_leapfrog_summary_for_guid(seg["plex_guid"])

    return {"ok": True}


@router.delete("/titles/{plex_guid:path}/segments")
async def delete_all_segments_for_title(plex_guid: str):
    """Delete all segments for a specific title."""
    thumbnail_paths = await db.get_thumbnail_paths_for_guid(plex_guid)
    deleted = await db.delete_segments_for_guid(plex_guid)
    _delete_thumbnail_files(thumbnail_paths)
    _refresh_leapfrog_summary_for_guid(plex_guid)
    return {"ok": True, "deleted": deleted}


@router.post("/segments/clear-all")
async def clear_all_segments(body: ClearSegmentsRequest | None = None):
    """Delete all stored segments, thumbnails, and optionally reset scan state."""

    payload = body or ClearSegmentsRequest()
    thumbnail_paths = await db.get_all_thumbnail_paths()
    if payload.clear_queue:
        await db.cancel_all_queued_items()
    deleted = await db.delete_all_segments()
    reset_jobs = await db.reset_all_scan_jobs() if payload.reset_scan_state else 0
    _delete_thumbnail_files(thumbnail_paths)
    logger.warning(
        "Cleared %d stored segments and %d thumbnail path(s); reset_scan_state=%s",
        deleted,
        len(thumbnail_paths),
        payload.reset_scan_state,
    )
    return {
        "ok": True,
        "deleted": deleted,
        "deleted_thumbnails": len(thumbnail_paths),
        "reset_scan_jobs": reset_jobs,
    }


@router.get("/segments")
async def get_all_segments(limit: int = 100, offset: int = 0):
    segments = await db.get_all_segments(limit=limit, offset=offset)
    return {"segments": segments}
