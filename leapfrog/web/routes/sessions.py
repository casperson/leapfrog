from fastapi import APIRouter, HTTPException

from ...adapters.plex_runtime import resolve_plex_playback_context
from ...logger import get_logger
from ...preferences import (
    get_preference_threshold_settings,
    get_resolved_preferences_for_users,
)
import leapfrog.plex_client as plex_mod
from ...watcher import skip_events
from ... import database as db
from ...scanner import get_queue_size, get_current_scan, get_current_scans, get_worker_pool_size, is_paused

logger = get_logger(__name__)
router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def get_sessions():
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        return {"sessions": [], "error": "Plex not configured"}

    sessions = await client.get_active_sessions()
    nudity_threshold, profanity_threshold = await get_preference_threshold_settings()
    resolved_preferences = await get_resolved_preferences_for_users(
        (session.user for session in sessions),
        nudity_threshold=nudity_threshold,
        profanity_threshold=profanity_threshold,
    )
    result = []
    for s in sessions:
        preferences = resolved_preferences.get(s.user, {})
        filtering_enabled = any(bool(pref["enabled"]) for pref in preferences.values())
        result.append({
            "session_key": s.session_key,
            "user": s.user,
            "title": s.full_title,
            "media_type": s.media_type,
            "position_ms": s.position_ms,
            "duration_ms": s.duration_ms,
            "client": s.client_title,
            "is_controllable": s.is_controllable,
            "filtering_enabled": filtering_enabled,
            "enabled_categories": [
                category for category, pref in preferences.items() if bool(pref["enabled"])
            ],
            "thumb_url": client.thumb_url(s.thumb) if s.thumb else "",
        })
    return {"sessions": result}


@router.get("/events")
async def get_skip_events():
    return {"events": list(skip_events)}


@router.get("/{session_key}/adapter-status")
async def get_session_adapter_status(session_key: str):
    """Return Plex adapter status for one active playback session."""
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        return {"connected": False, "error": "Plex not configured"}

    sessions = await client.get_active_sessions()
    session = next((s for s in sessions if s.session_key == session_key), None)
    if session is None:
        raise HTTPException(status_code=404, detail="Active session not found")

    context = await resolve_plex_playback_context(session)
    return context.to_status_record()


@router.get("/scanner-status")
async def scanner_status():
    current_guid = get_current_scan()
    current_title = None
    current_progress = 0.0
    active_scans: list[dict] = []

    current_guids = get_current_scans()
    # Fetch all active scan jobs in a single IN query rather than one query per guid.
    jobs_by_guid = await db.get_scan_jobs_by_guids(current_guids)
    for guid in current_guids:
        job = jobs_by_guid.get(guid)
        if not job:
            continue
        active_scans.append({
            "guid": guid,
            "title": job.get("title") or guid,
            "progress": float(job.get("progress") or 0.0),
            "status": job.get("status") or "scanning",
        })

    if current_guid:
        job = jobs_by_guid.get(current_guid)
        if job:
            current_title = job["title"]
            current_progress = job["progress"]

    configured_workers = max(1, int(await db.get_setting("scan_workers", "2")))
    effective_workers = max(1, int(get_worker_pool_size()))
    active_workers = len(active_scans)

    return {
        "queue_size": get_queue_size(),
        "current_scan": current_guid,
        "current_title": current_title,
        "current_progress": current_progress,
        "current_scans": current_guids,
        "active_scans": active_scans,
        "workers_configured": effective_workers,
        "workers_target": configured_workers,
        "workers_active": active_workers,
        "workers_idle": max(0, effective_workers - active_workers),
        "paused": is_paused(),
    }


@router.post("/{session_key}/skip")
async def skip_session_title(session_key: str):
    """Skip active playback to the end of the current (or next) detected segment."""
    try:
        client = plex_mod.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Plex not configured")

    sessions = await client.get_active_sessions()
    session = next((s for s in sessions if s.session_key == session_key), None)
    if session is None:
        raise HTTPException(status_code=404, detail="Active session not found")

    if not session.is_controllable:
        raise HTTPException(status_code=409, detail="Session is not remotely controllable")

    context = await resolve_plex_playback_context(session)
    segments = [dict(segment) for segment in context.effective_segments]
    if not context.all_segments:
        raise HTTPException(status_code=404, detail="No detected segments found for this title")
    if not segments:
        raise HTTPException(status_code=404, detail="No enabled segments found for this user")

    # Expand segment boundaries by 5 seconds before and after
    for seg in segments:
        seg["start_ms"] = max(0, int(seg["start_ms"]) - 5000)
        seg["end_ms"] = int(seg["end_ms"]) + 5000

    pos = int(session.position_ms)

    # Prefer the segment currently playing; otherwise choose the next segment ahead.
    current = next((seg for seg in segments if int(seg["start_ms"]) <= pos <= int(seg["end_ms"])), None)
    target_seg = current or next((seg for seg in segments if int(seg["start_ms"]) > pos), None)
    if target_seg is None:
        raise HTTPException(status_code=409, detail="No remaining segments ahead of current position")

    skip_buffer_ms = int(await db.get_setting("skip_buffer_ms", "3000"))
    # Seek to the expanded segment start
    seek_to_ms = int(target_seg["start_ms"])

    ok = await client.seek(
        session.client_identifier,
        seek_to_ms,
        session.client_address,
        session.client_port,
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to seek Plex client")

    return {
        "ok": True,
        "session_key": session.session_key,
        "title": session.full_title,
        "seek_to_ms": seek_to_ms,
        "segment_start_ms": int(target_seg["start_ms"]),
        "segment_end_ms": int(target_seg["end_ms"]),
        "client": session.client_title,
        "user": session.user,
        "adapter_status": context.to_status_record(),
    }
