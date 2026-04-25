from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...logger import get_logger
import leapfrog.plex_client as plex_mod
from ... import database as db
from ... import scanner as scan_mod

logger = get_logger(__name__)
router = APIRouter(prefix="/api/scan", tags=["scanner"])


class ScanTitleRequest(BaseModel):
    """Request body for scanning a title."""

    plex_guid: str
    now: bool = False
    library_id: str | None = None


class ScanLibraryRequest(BaseModel):
    """Request body for scanning a library."""

    now: bool = False


class QueueUnscannedRequest(BaseModel):
    """Request body for explicitly queueing unscanned titles."""

    media_type: str = "movie"
    now: bool = False


class SkipCurrentScanRequest(BaseModel):
    plex_guid: str | None = None


class QueueReorderRequest(BaseModel):
    ordered_guids: list[str] = Field(default_factory=list)


class QueueSelectionRequest(BaseModel):
    guids: list[str] = Field(default_factory=list)


class ToggleIgnoredRequest(BaseModel):
    ignored: bool


async def _queue_payload() -> dict:
    queued_jobs = await db.get_queue_snapshot()
    current_guids = scan_mod.get_current_scans()
    jobs_by_guid = await db.get_scan_jobs_by_guids(current_guids)
    active_scans = [
        {
            "guid": guid,
            "title": jobs_by_guid.get(guid, {}).get("title") or guid,
            "status": jobs_by_guid.get(guid, {}).get("status") or "scanning",
            "progress": float(jobs_by_guid.get(guid, {}).get("progress") or 0.0),
            "cancel_requested": bool(jobs_by_guid.get(guid, {}).get("cancel_requested", 0)),
        }
        for guid in current_guids
    ]
    return {
        "jobs": queued_jobs,
        "queue_size": len(queued_jobs),
        "current": scan_mod.get_current_scan(),
        "currents": current_guids,
        "active_scans": active_scans,
        "paused": scan_mod.is_paused(),
    }


async def _ensure_scan_job(plex_guid: str, library_id: str | None = None) -> dict:
    job = await db.get_scan_job_by_guid(plex_guid)
    if job:
        return job
    if not library_id:
        raise HTTPException(status_code=404, detail="Title not found")
    try:
        client = plex_mod.get_client()
        items = await client.get_library_items(library_id)
        item = next((entry for entry in items if entry.plex_guid == plex_guid), None)
        if not item:
            raise HTTPException(status_code=404, detail="Title not found")
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
        job = await db.get_scan_job_by_guid(plex_guid)
        if not job:
            raise HTTPException(status_code=404, detail="Title not found")
        return job
    except RuntimeError as exc:
        logger.error("Plex client error: %s", exc)
        raise HTTPException(status_code=404, detail="Title not found") from exc


@router.get("/queue")
async def get_scan_queue():
    return await _queue_payload()


@router.post("/title")
async def scan_title(body: ScanTitleRequest):
    """Queue a single title for scanning. If now=true, move to the queue top."""

    plex_guid = body.plex_guid
    await _ensure_scan_job(plex_guid, body.library_id)

    logger.info("Queueing title %s for scan (now=%s)", plex_guid, body.now)
    await db.reset_scan_job(plex_guid)
    await db.reset_media_scan_state(plex_guid)
    if body.now:
        await scan_mod.force_scan_job(plex_guid)
    else:
        await scan_mod.enqueue(plex_guid)
    payload = await _queue_payload()
    return {"ok": True, "queued": plex_guid, **payload}


@router.post("/library/{library_id}")
async def scan_library(library_id: str, body: ScanLibraryRequest):
    """Queue all eligible titles in a library for scanning."""

    jobs = await db.get_scan_jobs_by_library(library_id)
    if not jobs:
        try:
            client = plex_mod.get_client()
            items = await client.get_library_items(library_id)
            for item in items:
                if item.file_path:
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
            jobs = await db.get_scan_jobs_by_library(library_id)
        except RuntimeError:
            return {"ok": False, "error": "Plex not configured"}

    queued = 0
    for job in jobs:
        if job["status"] in ("pending", "failed"):
            await db.reset_scan_job(job["plex_guid"])
            await db.reset_media_scan_state(job["plex_guid"])
            if body.now:
                await scan_mod.force_scan_job(job["plex_guid"])
            else:
                await scan_mod.enqueue(job["plex_guid"])
            queued += 1

    payload = await _queue_payload()
    return {"ok": True, "queued": queued, **payload}


@router.post("/pause")
async def pause_scanner():
    scan_mod.pause_scanner()
    return {"ok": True, "paused": True}


@router.post("/resume")
async def resume_scanner():
    scan_mod.resume_scanner()
    return {"ok": True, "paused": False}


@router.post("/skip-current")
async def skip_current_scan(body: SkipCurrentScanRequest | None = None):
    """Skip or cancel the currently-running title; it remains pending for a future scan."""

    target_guid = body.plex_guid if body and body.plex_guid else scan_mod.get_current_scan()
    if not target_guid:
        raise HTTPException(status_code=404, detail="No scan in progress")
    await db.request_scan_cancel(target_guid)
    if body and body.plex_guid:
        if not scan_mod.request_skip_scan(body.plex_guid):
            raise HTTPException(status_code=404, detail="Requested scan is not currently active")
    else:
        scan_mod.skip_current_scan()
    return {"ok": True, "skipped": target_guid}


@router.post("/active/{plex_guid:path}/cancel")
async def cancel_active_scan(plex_guid: str):
    """Cancel one active scan by guid."""

    await db.request_scan_cancel(plex_guid)
    if not scan_mod.request_skip_scan(plex_guid):
        raise HTTPException(status_code=404, detail="Requested scan is not currently active")
    return {"ok": True, "canceled": plex_guid}


@router.post("/restart-scanner")
async def restart_scanner():
    """Trigger a clean scanner pool restart."""

    await scan_mod.request_scanner_restart()
    return {"ok": True}


@router.post("/reorder-queue")
async def reorder_queue():
    """Rebuild the queue using the default backend ordering."""

    await scan_mod.enqueue_pending()
    payload = await _queue_payload()
    return {"ok": True, **payload}


@router.post("/queue-unscanned")
async def queue_unscanned_titles(body: QueueUnscannedRequest):
    """Queue pending scan jobs by media type without doing any automatic discovery."""

    media_type = body.media_type.strip().lower()
    if media_type == "all":
        selected_media_type = None
    elif media_type in {"movie", "episode"}:
        selected_media_type = media_type
    else:
        raise HTTPException(status_code=422, detail="media_type must be movie, episode, or all")

    queued = await scan_mod.enqueue_pending(selected_media_type, force=body.now)
    payload = await _queue_payload()
    return {"ok": True, "queued": queued, **payload}


@router.post("/queue/reorder")
async def reorder_queue_snapshot(body: QueueReorderRequest):
    """Replace the queued order with an explicit guid list."""

    await db.replace_queue_snapshot(body.ordered_guids)
    payload = await _queue_payload()
    return {"ok": True, **payload}


@router.post("/queue/{plex_guid:path}/move-top")
async def move_queue_item_to_top(plex_guid: str):
    await db.move_queue_item(plex_guid, "top")
    payload = await _queue_payload()
    return {"ok": True, **payload}


@router.post("/queue/{plex_guid:path}/move-bottom")
async def move_queue_item_to_bottom(plex_guid: str):
    await db.move_queue_item(plex_guid, "bottom")
    payload = await _queue_payload()
    return {"ok": True, **payload}


@router.post("/queue/{plex_guid:path}/move-up")
async def move_queue_item_up(plex_guid: str):
    await db.move_queue_item(plex_guid, "up")
    payload = await _queue_payload()
    return {"ok": True, **payload}


@router.post("/queue/{plex_guid:path}/move-down")
async def move_queue_item_down(plex_guid: str):
    await db.move_queue_item(plex_guid, "down")
    payload = await _queue_payload()
    return {"ok": True, **payload}


@router.post("/queue/{plex_guid:path}/cancel")
async def cancel_queue_item(plex_guid: str):
    canceled = await db.cancel_queue_item(plex_guid)
    if not canceled:
        raise HTTPException(status_code=404, detail="Queued item not found")
    payload = await _queue_payload()
    return {"ok": True, "canceled": 1, **payload}


@router.post("/queue/cancel-selected")
async def cancel_selected_queue_items(body: QueueSelectionRequest):
    canceled = await db.cancel_queue_items(body.guids)
    payload = await _queue_payload()
    return {"ok": True, "canceled": canceled, **payload}


@router.post("/queue/cancel-all")
async def cancel_all_queue_items():
    canceled = await db.cancel_all_queued_items()
    payload = await _queue_payload()
    return {"ok": True, "canceled": canceled, **payload}


@router.post("/title/{plex_guid:path}/ignore")
async def toggle_title_ignored(plex_guid: str, body: ToggleIgnoredRequest):
    """Mark a title as ignored or re-enable it."""

    job = await db.get_scan_job_by_guid(plex_guid)
    if not job:
        raise HTTPException(status_code=404, detail="Title not found")

    await db.set_ignored(plex_guid, body.ignored)
    return {"ok": True, "plex_guid": plex_guid, "ignored": body.ignored}
