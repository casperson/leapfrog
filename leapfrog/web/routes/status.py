"""Application health and readiness routes."""

from __future__ import annotations

from fastapi import APIRouter

from ... import __version__, database as db
from ...adapters import list_runtime_adapters, list_segment_adapters
from ...adapters.runtime_common import SUPPORTED_SIDECAR_SUFFIXES
from ...config import Config
from ...scanner import get_current_scans, get_queue_size, get_worker_pool_size, is_paused
import leapfrog.plex_client as plex_mod

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
async def get_application_status():
    """Return concise application, scanner, adapter, and export health."""
    config = await Config.load()

    plex_connected = False
    plex_detail = "Plex not configured"
    if config.is_configured():
        try:
            client = plex_mod.get_client()
            plex_connected, plex_detail = await client.test_connection()
        except Exception as exc:
            plex_detail = str(exc)

    current_scans = get_current_scans()
    jobs_by_guid = await db.get_scan_jobs_by_guids(current_scans)
    active_scans = [
        {
            "guid": guid,
            "title": jobs_by_guid.get(guid, {}).get("title") or guid,
            "status": jobs_by_guid.get(guid, {}).get("status") or "scanning",
        }
        for guid in current_scans
    ]

    return {
        "version": __version__,
        "scanner": {
            "healthy": True,
            "paused": is_paused(),
            "queue_size": get_queue_size(),
            "worker_pool_size": get_worker_pool_size(),
            "active_scans": active_scans,
        },
        "adapters": {
            "plex": {
                "runtime_supported": True,
                "native_connected": plex_connected,
                "detail": plex_detail,
            },
            "emby": {
                "runtime_supported": True,
                "native_connected": False,
                "detail": "Runtime adapter available. Native Emby client hooks are not wired yet.",
            },
            "jellyfin": {
                "runtime_supported": True,
                "native_connected": False,
                "detail": "Runtime adapter available. Native Jellyfin client hooks are not wired yet.",
            },
        },
        "exports": {
            "healthy": True,
            "canonical_format": "leapfrog.segment.export/v1",
            "sidecar_suffixes": list(SUPPORTED_SIDECAR_SUFFIXES),
            "segment_adapters": list_segment_adapters(),
            "runtime_adapters": list_runtime_adapters(),
        },
    }
