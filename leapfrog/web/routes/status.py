"""Application health and readiness routes."""

from __future__ import annotations

from fastapi import APIRouter

from ... import __version__, database as db
from ...adapters import list_runtime_adapters, list_segment_adapters
from ...adapters.runtime_common import SUPPORTED_SIDECAR_SUFFIXES
from ...config import Config
from ...scanner import get_current_scans, get_queue_size, get_worker_pool_size, is_paused
from ...server_runtime import get_client
from .sessions import _build_skipper_status

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
async def get_application_status():
    """Return concise application, scanner, adapter, and export health."""
    config = await Config.load()

    server_connected = False
    server_detail = "Media server not configured"
    if config.is_configured():
        try:
            client = get_client()
            server_connected, server_detail = await client.test_connection()
        except Exception as exc:
            server_detail = str(exc)

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
        "skipper": await _build_skipper_status(),
        "adapters": {
            "plex": {
                "runtime_supported": True,
                "native_connected": config.server_type == "plex" and server_connected,
                "detail": server_detail if config.server_type == "plex" else "Available when Plex is active.",
            },
            "emby": {
                "runtime_supported": True,
                "native_connected": False,
                "detail": "Runtime adapter available. Native Emby client hooks are not wired yet.",
            },
            "jellyfin": {
                "runtime_supported": True,
                "native_connected": config.server_type == "jellyfin" and server_connected,
                "detail": server_detail if config.server_type == "jellyfin" else "Available when Jellyfin is active.",
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
