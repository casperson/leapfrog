from fastapi import APIRouter
from pydantic import BaseModel

from ...logger import configure_log_buffer, get_logger
from ... import database as db
from ...config import Config
import leapfrog.plex_client as plex_mod
from ... import scanner as scan_mod
from ...detectors.semantic import ensure_semantic_model_async
from ...preferences import get_category_metadata
from .segments import _invalidate_scan_labels_cache

logger = get_logger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])

DETECTOR_LABELS = [
    # EXPOSED categories
    "FEMALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "FEET_EXPOSED",
    "BELLY_EXPOSED",
    "ARMPITS_EXPOSED",
    # COVERED categories
    "FEMALE_GENITALIA_COVERED",
    "FEMALE_BREAST_COVERED",
    "MALE_BREAST_COVERED",
    "ANUS_COVERED",
    "BUTTOCKS_COVERED",
    "FEET_COVERED",
    "BELLY_COVERED",
    "ARMPITS_COVERED",
    # FACE categories
    "FACE_FEMALE",
    "FACE_MALE",
]


class SettingsPayload(BaseModel):
    plex_url: str | None = None
    plex_token: str | None = None
    poll_interval: str | None = None
    confidence_threshold: str | None = None
    skip_buffer_ms: str | None = None
    scan_step_ms: str | None = None
    scan_workers: str | None = None
    nudenet_model: str | None = None
    nudenet_model_path: str | None = None
    semantic_model_repo: str | None = None
    semantic_processor_repo: str | None = None
    semantic_model_variant: str | None = None
    segment_gap_ms: str | None = None
    segment_min_hits: str | None = None
    image_segment_max_ms: str | None = None
    profanity_terms: str | None = None
    profanity_allowlist: str | None = None
    default_profanity_threshold: str | None = None
    sexual_content_detection_threshold: str | None = None
    violence_detection_threshold: str | None = None
    drugs_detection_threshold: str | None = None
    profanity_merge_gap_ms: str | None = None
    whisper_enabled: str | None = None
    whisper_model: str | None = None
    log_buffer_capacity: str | None = None
    scan_window_start: str | None = None
    scan_window_end: str | None = None
    log_level: str | None = None
    excluded_library_ids: str | None = None
    scan_ratings: str | None = None
    scan_labels: str | None = None
    default_skip_labels: str | None = None
    semantic_detection_labels: str | None = None


class ValidateModelPathPayload(BaseModel):
    nudenet_model: str = "640m"
    nudenet_model_path: str = ""


@router.get("")
async def get_settings():
    return await db.get_all_settings()


@router.get("/categories")
async def get_categories():
    """Return the canonical category metadata for backend/frontend consumers."""
    return {"categories": await get_category_metadata()}


@router.get("/plex-server-id")
async def get_plex_server_id():
    """Return the Plex server machine identifier for constructing web deep links."""
    try:
        client = plex_mod.get_client()
        machine_id = await client.get_machine_identifier()
        return {"machine_identifier": machine_id}
    except RuntimeError:
        return {"machine_identifier": ""}
    except Exception as exc:
        logger.warning("Failed to get machine identifier: %s", exc)
        return {"machine_identifier": ""}


@router.put("")
async def update_settings(payload: SettingsPayload):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    
    # Check if scan_workers is being changed
    scan_workers_changed = False
    if "scan_workers" in data:
        current_workers = await db.get_setting("scan_workers", "2")
        if data["scan_workers"] != current_workers:
            scan_workers_changed = True
            logger.info("Scan workers changing from %s to %s", current_workers, data["scan_workers"])
    
    await db.update_settings(data)

    # Invalidate scan_labels cache so the next segment expand reflects the new value.
    if "scan_labels" in data:
        _invalidate_scan_labels_cache()

    # Reinitialise client if connection details changed
    if "plex_url" in data or "plex_token" in data:
        settings = await db.get_all_settings()
        url = settings.get("plex_url", "")
        token = settings.get("plex_token", "")
        if url and token:
            plex_mod.init_client(url, token)

    if "log_buffer_capacity" in data:
        configure_log_buffer(int(data["log_buffer_capacity"]))
    
    # Restart scanner pool if worker count changed
    if scan_workers_changed:
        await scan_mod.request_scanner_restart()
    
    return {"ok": True}


@router.post("/test-connection")
async def test_connection():
    settings = await db.get_all_settings()
    url = settings.get("plex_url", "")
    token = settings.get("plex_token", "")
    if not url or not token:
        return {"ok": False, "message": "Plex URL and token are required"}
    client = plex_mod.PlexClient(url, token)
    ok, message = await client.test_connection()
    return {"ok": ok, "message": message}


@router.get("/detector-labels")
async def get_detector_labels():
    return {"labels": DETECTOR_LABELS}


@router.post("/validate-model-path")
async def validate_model_path(payload: ValidateModelPathPayload):
    model_name = (payload.nudenet_model or "640m").strip().lower()

    # 320n is bundled with nudenet package; no download required.
    if not model_name.startswith("640"):
        return {
            "ok": True,
            "message": "320n uses bundled model; no custom file path required.",
        }

    try:
        # Scanner auto-downloads 640m model into app storage on first use.
        from ... import scanner as scanner_mod

        path = scanner_mod._ensure_local_640m_model()
        if not path:
            return {
                "ok": False,
                "message": "Could not download 640m model automatically. Check internet access and try again.",
            }
        return {
            "ok": True,
            "message": "640m model is ready in local app storage.",
        }
    except Exception as exc:
        logger.warning("NudeNet auto-download validation failed: %s", exc)
        return {
            "ok": False,
            "message": f"Model could not be prepared: {exc}",
        }


@router.post("/prepare-semantic-model")
async def prepare_semantic_model():
    """Download and validate the local semantic ONNX model assets."""
    try:
        config = await Config.load()
        await ensure_semantic_model_async(config)
        return {
            "ok": True,
            "message": "Semantic ONNX model is ready in local app storage.",
        }
    except Exception as exc:
        logger.warning("Semantic model preparation failed: %s", exc)
        return {
            "ok": False,
            "message": f"Semantic model could not be prepared: {exc}",
        }
