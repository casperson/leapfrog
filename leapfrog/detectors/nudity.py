"""NudeNet-backed nudity detector."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable

import httpx

from ..domain import MediaScanTarget, MediaSegment
from ..frame_extractor import extract_frames_batch, get_duration_ms
from ..logger import get_logger
from .base import DetectorResult, ProgressCallback

logger = get_logger(__name__)

THUMBNAILS_DIR: Path = Path.home() / ".leapfrog" / "thumbnails"
MODELS_DIR: Path = Path.home() / ".leapfrog" / "models"
NUDENET_640_MODEL_FILENAME = "640m.onnx"
NUDENET_640_DOWNLOAD_URLS = [
    "https://github.com/notAI-tech/NudeNet/releases/download/v3/640m.onnx",
    "https://github.com/notAI-tech/NudeNet/releases/latest/download/640m.onnx",
]

_thread_local = threading.local()
_model_download_lock = asyncio.Lock()


def _get_detector(model_name: str = "320n", model_path: str = ""):
    model_name_normalized = (model_name or "320n").strip().lower()
    detector = getattr(_thread_local, "nude_detector", None)
    detector_key = getattr(_thread_local, "nude_detector_key", None)

    requested_resolution = 640 if model_name_normalized.startswith("640") else 320
    selected_model_path = (model_path or "").strip()
    if requested_resolution == 640 and not selected_model_path:
        candidate = MODELS_DIR / NUDENET_640_MODEL_FILENAME
        if candidate.is_file() and candidate.stat().st_size > 0:
            selected_model_path = str(candidate)
        else:
            logger.warning("640m model not found locally; falling back to bundled 320n")
            requested_resolution = 320

    if selected_model_path and not os.path.isfile(selected_model_path):
        logger.warning(
            "Configured NudeNet model path not found: %s; falling back to bundled 320n",
            selected_model_path,
        )
        selected_model_path = ""
        requested_resolution = 320

    current_key = (requested_resolution, selected_model_path)
    if detector is None or detector_key != current_key:
        try:
            from nudenet import NudeDetector

            kwargs = {"inference_resolution": requested_resolution}
            if selected_model_path:
                kwargs["model_path"] = selected_model_path
            detector = NudeDetector(**kwargs)
            _thread_local.nude_detector = detector
            _thread_local.nude_detector_key = current_key
            logger.info("NudeNet detector loaded in thread %s", threading.get_ident())
        except ImportError:
            logger.error("nudenet package not installed. Run: pip install nudenet")
            raise
    return detector


def ensure_local_640m_model() -> str:
    """Blocking download of the NudeNet 640m model."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    target = MODELS_DIR / NUDENET_640_MODEL_FILENAME
    if target.is_file() and target.stat().st_size > 0:
        return str(target)

    for url in NUDENET_640_DOWNLOAD_URLS:
        try:
            logger.info("Downloading NudeNet 640m model from %s", url)
            with httpx.Client(timeout=120.0, follow_redirects=True) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with open(target, "wb") as out:
                        for chunk in resp.iter_bytes():
                            if chunk:
                                out.write(chunk)

            if target.is_file() and target.stat().st_size > 0:
                logger.info("Downloaded NudeNet 640m model to %s", target)
                return str(target)
        except Exception as exc:
            logger.warning("Failed to download NudeNet 640m model from %s: %s", url, exc)

    if target.exists() and target.stat().st_size == 0:
        target.unlink(missing_ok=True)
    return ""


async def ensure_640m_model_async() -> str:
    """Ensure the 640m model file exists before a scan starts."""
    target = MODELS_DIR / NUDENET_640_MODEL_FILENAME
    if target.is_file() and target.stat().st_size > 0:
        return str(target)

    async with _model_download_lock:
        if target.is_file() and target.stat().st_size > 0:
            return str(target)
        return await asyncio.to_thread(ensure_local_640m_model)


def _classify_frame(
    jpeg_bytes: bytes,
    threshold: float,
    enabled_labels: set[str],
    model_name: str,
    model_path: str,
) -> tuple[bool, float, list[str]]:
    """Return whether the frame matches the enabled NudeNet labels."""
    try:
        detector = _get_detector(model_name=model_name, model_path=model_path)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_file:
            temp_file.write(jpeg_bytes)
            temp_path = temp_file.name
        try:
            results = detector.detect(temp_path)
        finally:
            os.unlink(temp_path)

        if not results:
            return False, 0.0, []

        max_score = 0.0
        detected: list[str] = []
        for detection in results:
            label = detection.get("class")
            score = float(detection.get("score", 0.0))
            if label in enabled_labels:
                if label not in detected:
                    detected.append(label)
                if score > max_score:
                    max_score = score
        return max_score >= threshold, max_score, detected
    except Exception as exc:
        logger.debug("Classification error: %s", exc)
        return False, 0.0, []


class NudityDetector:
    """Scan frames with NudeNet and emit nudity segments."""

    category = "nudity"

    def __init__(
        self,
        *,
        should_abort: Callable[[str], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        should_stop_for_window: Callable[[], bool] | None = None,
    ) -> None:
        self._should_abort = should_abort
        self._should_pause = should_pause
        self._should_stop_for_window = should_stop_for_window

    async def scan(
        self,
        target: MediaScanTarget,
        config,
        progress_callback: ProgressCallback | None = None,
    ) -> DetectorResult:
        """Run frame extraction and NudeNet inference for one media item."""
        duration_ms = await get_duration_ms(target.file_path)
        if not duration_ms:
            return DetectorResult(
                category=self.category,
                source="nudenet",
                status="failed",
                detail="Could not determine video duration.",
            )

        step_ms = max(1000, int(getattr(config, "scan_step_ms", 5000)))
        total_steps = max(1, duration_ms // step_ms)
        gap_ms = max(1000, int(getattr(config, "segment_gap_ms", 12000)))
        min_hits = max(1, int(getattr(config, "segment_min_hits", 1)))
        threshold = float(getattr(config, "confidence_threshold", 0.6))
        enabled_labels = set(getattr(config, "scan_labels", []))
        nudenet_model = str(getattr(config, "nudenet_model", "320n"))
        nudenet_model_path = str(getattr(config, "nudenet_model_path", ""))

        if nudenet_model.startswith("640") and not nudenet_model_path:
            nudenet_model_path = await ensure_640m_model_async()
            if not nudenet_model_path:
                logger.warning(
                    "Could not prepare 640m model for %s; falling back to 320n",
                    target.title,
                )
                nudenet_model = "320n"

        THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

        segments: list[MediaSegment] = []
        cluster_start_ms: int | None = None
        cluster_prev_ms = 0
        cluster_hit_count = 0
        cluster_best_jpeg = b""
        cluster_best_score = 0.0
        cluster_detected_labels: list[str] = []

        async def flush_cluster() -> None:
            nonlocal cluster_start_ms, cluster_prev_ms, cluster_hit_count
            nonlocal cluster_best_jpeg, cluster_best_score, cluster_detected_labels

            if cluster_start_ms is None:
                return
            if cluster_hit_count >= min_hits:
                thumb_path = ""
                if cluster_best_jpeg:
                    filename = f"{target.media_id.replace('/', '_')}_{cluster_start_ms}.jpg"
                    full_path = THUMBNAILS_DIR / filename
                    full_path.write_bytes(cluster_best_jpeg)
                    thumb_path = str(full_path)
                segments.append(
                    MediaSegment(
                        media_id=target.media_id,
                        title=target.title,
                        start_ms=cluster_start_ms,
                        end_ms=cluster_prev_ms + gap_ms,
                        category=self.category,
                        source="nudenet",
                        confidence=cluster_best_score,
                        thumbnail_path=thumb_path,
                        labels=",".join(cluster_detected_labels),
                    )
                )

            cluster_start_ms = None
            cluster_prev_ms = 0
            cluster_hit_count = 0
            cluster_best_jpeg = b""
            cluster_best_score = 0.0
            cluster_detected_labels = []

        async for offset_ms, jpeg in extract_frames_batch(target.file_path, step_ms, duration_ms):
            idx = offset_ms // step_ms
            if self._should_abort and self._should_abort(target.media_id):
                return DetectorResult(
                    category=self.category,
                    source="nudenet",
                    status="pending_skip",
                    detail="Scan skipped by user request.",
                )
            if self._should_pause and self._should_pause():
                return DetectorResult(
                    category=self.category,
                    source="nudenet",
                    status="pending_pause",
                    detail="Scanner paused mid-scan.",
                )
            if (
                idx % 5 == 0
                and not target.force_scan
                and self._should_stop_for_window
                and self._should_stop_for_window()
            ):
                return DetectorResult(
                    category=self.category,
                    source="nudenet",
                    status="pending_window",
                    detail="Scan window ended during scan.",
                )

            if jpeg:
                is_match, score, labels = await asyncio.to_thread(
                    _classify_frame,
                    jpeg,
                    threshold,
                    enabled_labels,
                    nudenet_model,
                    nudenet_model_path,
                )
                if is_match:
                    if cluster_start_ms is None:
                        cluster_start_ms = offset_ms
                        cluster_prev_ms = offset_ms
                        cluster_hit_count = 1
                        cluster_best_jpeg = jpeg
                        cluster_best_score = score
                        cluster_detected_labels = labels.copy()
                    elif offset_ms - cluster_prev_ms > gap_ms:
                        await flush_cluster()
                        cluster_start_ms = offset_ms
                        cluster_prev_ms = offset_ms
                        cluster_hit_count = 1
                        cluster_best_jpeg = jpeg
                        cluster_best_score = score
                        cluster_detected_labels = labels.copy()
                    else:
                        cluster_prev_ms = offset_ms
                        cluster_hit_count += 1
                        if score > cluster_best_score:
                            cluster_best_jpeg = jpeg
                            cluster_best_score = score
                            cluster_detected_labels = labels.copy()
                        else:
                            for label in labels:
                                if label not in cluster_detected_labels:
                                    cluster_detected_labels.append(label)

            if progress_callback and idx % 30 == 0:
                await progress_callback((idx + 1) / total_steps)

        await flush_cluster()
        if progress_callback:
            await progress_callback(1.0)
        return DetectorResult(
            category=self.category,
            source="nudenet",
            status="done",
            segments=segments,
        )
