"""Local prompt-based image detectors for sexual content, violence, and drugs."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import Callable
from typing import Protocol

from PIL import Image, ImageFilter, ImageStat

from ..domain import MediaScanTarget, SampledFrame
from ..frame_extractor import get_duration_ms, sample_video_frames
from ..logger import get_logger
from .base import DetectorResult, ProgressCallback
from .clip_onnx import (
    SemanticModelUnavailableError,
    get_shared_clip_backend,
)
from .image_common import FrameHit, cluster_frame_hits

logger = get_logger(__name__)

SEMANTIC_PROMPT_BANK: dict[str, dict[str, tuple[str, ...]]] = {
    "sexual_content": {
        "kissing": (
            "a movie frame of two adults kissing passionately",
            "a romantic close-up kiss between adults",
            "an intimate kiss in a film scene",
        ),
        "intimate_touch": (
            "a movie scene with intimate touching between adults",
            "romantic intimate physical contact in a bedroom scene",
            "suggestive caressing between adults in a film scene",
        ),
        "bed_intimacy": (
            "an adult couple embracing in bed in a movie scene",
            "a sexual situation without explicit nudity in a film frame",
            "a movie frame showing implied sexual activity without nudity",
        ),
    },
    "violence": {
        "fight": (
            "a movie frame of a physical fight between people",
            "an action scene with punching kicking or brawling",
            "a violent struggle in a film scene",
        ),
        "blood": (
            "visible blood in a violent movie scene",
            "a bloody injury or gore in a film frame",
            "a wound with visible blood during violence",
        ),
        "weapon": (
            "a weapon pointed during an attack in a movie scene",
            "a gun knife or other weapon in a violent confrontation",
            "an armed threat in a film frame",
        ),
    },
    "drugs": {
        "smoke": (
            "drug smoke or inhaled substance use in a movie scene",
            "someone smoking a suspicious substance in a film frame",
            "recreational drug smoking in a close-up",
        ),
        "needle": (
            "drug injection with a needle in a movie scene",
            "a needle used for substance abuse in a film frame",
            "someone injecting an illicit drug",
        ),
        "pill": (
            "misuse of pills or tablets in a movie scene",
            "substance abuse with pills in a film frame",
            "someone abusing prescription pills",
        ),
        "drug_paraphernalia": (
            "drug paraphernalia on a table in a movie scene",
            "baggies pipes syringes or powder used for drugs",
            "a close-up of illicit drug equipment",
        ),
    },
}

SEMANTIC_NEGATIVE_PROMPTS: dict[str, tuple[str, ...]] = {
    "sexual_content": (
        "a neutral movie scene with no romance or intimacy",
        "people having an ordinary conversation in a film scene",
        "a landscape or room with no people touching",
    ),
    "violence": (
        "a calm movie scene with no fighting or weapons",
        "people standing peacefully in a film frame",
        "a neutral conversation scene with no blood or attack",
    ),
    "drugs": (
        "an ordinary movie scene with no drugs or paraphernalia",
        "a clean table with common household objects only",
        "people talking with no smoking injection or pills",
    ),
}

SEMANTIC_LABEL_THRESHOLDS: dict[str, float] = {
    "sexual_content": 0.30,
    "violence": 0.28,
    "drugs": 0.30,
}


class SemanticPromptBackend(Protocol):
    """Backend interface for local prompt-based image scoring."""

    def score_frame(
        self,
        jpeg_bytes: bytes,
        prompt_bank: dict[str, tuple[str, ...]],
        negative_prompts: tuple[str, ...] = (),
    ) -> dict[str, float]:
        """Return scores keyed by prompt label for one frame."""


@dataclass(slots=True)
class ImageFeatures:
    red_ratio: float
    pink_ratio: float
    gray_ratio: float
    dark_ratio: float
    bright_ratio: float
    edge_strength: float
    skin_ratio: float


class HeuristicSemanticBackend:
    """Pure-local fallback backend using image statistics as prompt proxies."""

    def score_frame(
        self,
        jpeg_bytes: bytes,
        prompt_bank: dict[str, tuple[str, ...]],
    ) -> dict[str, float]:
        try:
            features = self._extract_features(jpeg_bytes)
        except Exception as exc:
            logger.debug("Semantic backend could not score frame: %s", exc)
            return {label: 0.0 for label in prompt_bank}
        return {label: self._score_label(label, features) for label in prompt_bank}

    def _extract_features(self, jpeg_bytes: bytes) -> ImageFeatures:
        image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        pixels = list(image.getdata())
        total = max(1, len(pixels))

        red_ratio = sum(1 for r, g, b in pixels if r > 140 and r > g * 1.15 and r > b * 1.15) / total
        pink_ratio = sum(1 for r, g, b in pixels if r > 150 and g > 90 and b > 90 and r > b * 0.9) / total
        gray_ratio = sum(1 for r, g, b in pixels if max(r, g, b) - min(r, g, b) < 18) / total
        dark_ratio = sum(1 for r, g, b in pixels if max(r, g, b) < 70) / total
        bright_ratio = sum(1 for r, g, b in pixels if min(r, g, b) > 185) / total
        skin_ratio = sum(
            1
            for r, g, b in pixels
            if r > 95 and g > 40 and b > 20 and max(r, g, b) - min(r, g, b) > 15 and abs(r - g) > 15 and r > g and r > b
        ) / total
        edge_image = image.convert("L").filter(ImageFilter.FIND_EDGES)
        edge_strength = ImageStat.Stat(edge_image).mean[0] / 255.0
        return ImageFeatures(
            red_ratio=red_ratio,
            pink_ratio=pink_ratio,
            gray_ratio=gray_ratio,
            dark_ratio=dark_ratio,
            bright_ratio=bright_ratio,
            edge_strength=edge_strength,
            skin_ratio=skin_ratio,
        )

    def _score_label(self, label: str, features: ImageFeatures) -> float:
        if label == "kissing":
            return min(1.0, features.skin_ratio * 2.0 + features.pink_ratio * 2.4)
        if label == "intimate_touch":
            return min(1.0, features.skin_ratio * 2.4 + features.pink_ratio * 1.6)
        if label == "bed_intimacy":
            return min(1.0, features.skin_ratio * 1.8 + features.bright_ratio * 0.8 + features.gray_ratio * 0.5)
        if label == "fight":
            return min(1.0, features.edge_strength * 1.9 + features.dark_ratio * 0.5)
        if label == "blood":
            return min(1.0, features.red_ratio * 3.2 + features.edge_strength * 0.4)
        if label == "weapon":
            return min(1.0, features.dark_ratio * 1.6 + features.edge_strength * 1.1)
        if label == "smoke":
            return min(1.0, features.gray_ratio * 1.7 + features.bright_ratio * 0.8)
        if label == "needle":
            return min(1.0, features.edge_strength * 1.3 + features.gray_ratio * 0.8 + features.dark_ratio * 0.4)
        if label == "pill":
            return min(1.0, features.bright_ratio * 1.5 + features.pink_ratio * 0.6 + features.edge_strength * 0.4)
        if label == "drug_paraphernalia":
            return min(1.0, features.dark_ratio * 0.9 + features.gray_ratio * 1.1 + features.edge_strength * 0.9)
        return 0.0


class SemanticCategoryDetector:
    """Category-specific semantic detector built on a shared local prompt backend."""

    source = "semantic_clip"

    def __init__(
        self,
        category: str,
        *,
        backend: SemanticPromptBackend | None = None,
        should_abort: Callable[[str], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        should_stop_for_window: Callable[[], bool] | None = None,
    ) -> None:
        self.category = category
        self._backend = backend
        self._should_abort = should_abort
        self._should_pause = should_pause
        self._should_stop_for_window = should_stop_for_window

    async def scan(
        self,
        target: MediaScanTarget,
        config,
        progress_callback: ProgressCallback | None = None,
    ) -> DetectorResult:
        duration_ms = await get_duration_ms(target.file_path)
        if not duration_ms:
            return DetectorResult(
                category=self.category,
                source=self.source,
                status="failed",
                detail="Could not determine video duration.",
            )
        step_ms = max(1000, int(getattr(config, "scan_step_ms", 5000)))
        frames = await sample_video_frames(target.file_path, step_ms, duration_ms)
        return await self.scan_frames(
            target,
            frames,
            config,
            progress_callback=progress_callback,
        )

    async def scan_frames(
        self,
        target: MediaScanTarget,
        frames: list[SampledFrame],
        config,
        progress_callback: ProgressCallback | None = None,
    ) -> DetectorResult:
        prompt_bank = SEMANTIC_PROMPT_BANK[self.category]
        negative_prompts = SEMANTIC_NEGATIVE_PROMPTS[self.category]
        threshold = float(
            getattr(
                config,
                f"{self.category}_detection_threshold",
                SEMANTIC_LABEL_THRESHOLDS[self.category],
            )
        )
        gap_ms = max(1000, int(getattr(config, "segment_gap_ms", 12000)))
        min_hits = max(1, int(getattr(config, "segment_min_hits", 1)))
        total_steps = max(1, len(frames))
        backend = self._backend or get_semantic_backend(config)

        hits: list[FrameHit] = []
        for idx, frame in enumerate(frames):
            if self._should_abort and self._should_abort(target.media_id):
                return DetectorResult(
                    category=self.category,
                    source=self.source,
                    status="pending_skip",
                    detail="Scan skipped by user request.",
                )
            if self._should_pause and self._should_pause():
                return DetectorResult(
                    category=self.category,
                    source=self.source,
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
                    source=self.source,
                    status="pending_window",
                    detail="Scan window ended during scan.",
                )
            try:
                scores = await asyncio.to_thread(
                    backend.score_frame,
                    frame.jpeg_bytes,
                    prompt_bank,
                    negative_prompts,
                )
            except SemanticModelUnavailableError as exc:
                return DetectorResult(
                    category=self.category,
                    source=self.source,
                    status="failed",
                    detail=str(exc),
                )
            matched_labels = [
                label
                for label, score in scores.items()
                if float(score) >= threshold
            ]
            if matched_labels:
                best_score = max(float(scores[label]) for label in matched_labels)
                hits.append(
                    FrameHit(
                        offset_ms=frame.offset_ms,
                        confidence=best_score,
                        labels=matched_labels,
                        jpeg_bytes=frame.jpeg_bytes,
                    )
                )
            if progress_callback and idx % 30 == 0:
                await progress_callback((idx + 1) / total_steps)

        segments = cluster_frame_hits(
            target=target,
            category=self.category,
            source=self.source,
            hits=hits,
            gap_ms=gap_ms,
            min_hits=min_hits,
        )
        if progress_callback:
            await progress_callback(1.0)
        return DetectorResult(
            category=self.category,
            source=self.source,
            status="done",
            segments=segments,
            detail=(
                f"Scored {len(frames)} sampled frame(s) with local ONNX CLIP "
                f"against {len(prompt_bank)} prompt labels."
            ),
        )


class SexualContentDetector(SemanticCategoryDetector):
    def __init__(
        self,
        *,
        backend: SemanticPromptBackend | None = None,
        should_abort: Callable[[str], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        should_stop_for_window: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(
            "sexual_content",
            backend=backend,
            should_abort=should_abort,
            should_pause=should_pause,
            should_stop_for_window=should_stop_for_window,
        )


class ViolenceDetector(SemanticCategoryDetector):
    def __init__(
        self,
        *,
        backend: SemanticPromptBackend | None = None,
        should_abort: Callable[[str], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        should_stop_for_window: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(
            "violence",
            backend=backend,
            should_abort=should_abort,
            should_pause=should_pause,
            should_stop_for_window=should_stop_for_window,
        )


class DrugsDetector(SemanticCategoryDetector):
    def __init__(
        self,
        *,
        backend: SemanticPromptBackend | None = None,
        should_abort: Callable[[str], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        should_stop_for_window: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(
            "drugs",
            backend=backend,
            should_abort=should_abort,
            should_pause=should_pause,
            should_stop_for_window=should_stop_for_window,
        )


def get_semantic_backend(config) -> SemanticPromptBackend:
    """Return the shared semantic ONNX backend for the current configuration."""
    return get_shared_clip_backend(
        model_repo=str(getattr(config, "semantic_model_repo", "Xenova/clip-vit-base-patch32")),
        processor_repo=str(getattr(config, "semantic_processor_repo", "openai/clip-vit-base-patch32")),
        variant=str(getattr(config, "semantic_model_variant", "int8")),
    )


async def ensure_semantic_model_async(config) -> None:
    """Ensure the semantic ONNX model files are available locally."""
    backend = get_semantic_backend(config)
    await asyncio.to_thread(backend.ensure_ready)
