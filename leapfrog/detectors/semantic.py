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
        "explicit_sex": ("explicit sexual intercourse in a movie scene", "visible explicit sex act", "adults engaged in explicit sexual activity"),
        "simulated_sex": ("simulated sex or thrusting in a movie scene", "implied sex act without explicit nudity", "adults acting out sex in a film"),
        "oral_sex": ("oral sex activity in a movie scene", "a film frame suggesting oral sex", "visible oral sex act"),
        "masturbation": ("masturbation in a movie scene", "adult self stimulation in a film", "visible masturbation"),
        "sexual_touching": ("sexual touching of intimate body areas", "hands touching breasts buttocks or genitals sexually", "sexualized touching between adults"),
        "intimate_touch": ("intimate touching between adults", "romantic intimate physical contact", "suggestive caressing between adults"),
        "bed_intimacy": ("adult couple embracing in bed", "implied sexual activity in bed", "sexual situation without explicit nudity"),
        "heavy_making_out": ("two adults making out passionately", "extended passionate kissing", "heavy make out scene"),
        "romantic_kiss": ("romantic kiss between adults", "close romantic kiss", "two people kissing romantically"),
        "brief_kiss": ("brief peck kiss", "quick non explicit kiss", "short casual kiss"),
        "lingerie": ("sexualized lingerie scene", "adult wearing lingerie suggestively", "erotic underwear in a movie frame"),
        "striptease": ("striptease in a movie scene", "erotic undressing", "adult stripping clothes sexually"),
    },
    "violence": {
        "graphic_violence": ("graphic violent scene with gore", "extreme bloody violence", "graphic gore or mutilation"),
        "fight": ("physical fight between people", "punching kicking or brawling", "violent struggle in a film scene"),
        "blood": ("visible blood in a violent scene", "bloody injury or gore", "wound with visible blood"),
        "weapon_threat": ("weapon pointed during an attack", "gun knife or weapon in confrontation", "armed threat in a film frame"),
        "gunfire": ("gunfire in an action scene", "a gun being fired", "people shooting guns"),
        "stabbing": ("stabbing with a knife", "knife attack in a film", "someone being stabbed"),
        "explosion": ("explosion or blast", "fireball explosion", "violent explosion"),
        "dead_body": ("dead body in a movie scene", "corpse shown in a film", "body lying dead"),
        "disturbing_image": ("disturbing frightening image", "disturbing non graphic film frame", "unsettling scary image"),
        "medical_injury": ("medical treatment of an injury", "wound being treated", "medical injury scene"),
    },
    "drugs": {
        "hard_drug_use": ("visible hard drug use", "someone using illicit hard drugs", "explicit drug use in a movie"),
        "needle": ("drug injection with a needle", "needle used for substance abuse", "someone injecting an illicit drug"),
        "powder_drugs": ("white powder drugs on a table", "lines of cocaine or powder drugs", "powdered illicit drugs"),
        "pill_abuse": ("misuse of pills or tablets", "substance abuse with pills", "abusing prescription pills"),
        "drug_paraphernalia": ("drug paraphernalia on a table", "baggies pipes syringes or powder used for drugs", "illicit drug equipment"),
        "smoking_drugs": ("drug smoke or inhaled substance use", "smoking a suspicious substance", "recreational drug smoking"),
        "marijuana": ("marijuana or cannabis use", "smoking marijuana", "cannabis products in a movie"),
        "alcohol_abuse": ("heavy alcohol abuse", "person extremely drunk", "dangerous alcohol intoxication"),
        "tobacco": ("cigarette smoking", "smoking tobacco", "person smoking a cigarette"),
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
    "sexual_content": 0.45,
    "violence": 0.45,
    "drugs": 0.45,
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
        negative_prompts: tuple[str, ...] = (),
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
        if label in {"brief_kiss", "romantic_kiss"}:
            return min(1.0, features.skin_ratio * 2.0 + features.pink_ratio * 2.4)
        if label == "heavy_making_out":
            return min(1.0, features.skin_ratio * 2.3 + features.pink_ratio * 2.6)
        if label == "intimate_touch":
            return min(1.0, features.skin_ratio * 2.4 + features.pink_ratio * 1.6)
        if label in {"bed_intimacy", "lingerie"}:
            return min(1.0, features.skin_ratio * 1.8 + features.bright_ratio * 0.8 + features.gray_ratio * 0.5)
        if label in {"explicit_sex", "simulated_sex", "oral_sex", "masturbation", "sexual_touching", "striptease"}:
            return min(1.0, features.skin_ratio * 2.7 + features.pink_ratio * 2.0 + features.bright_ratio * 0.4)
        if label == "graphic_violence":
            return min(1.0, features.red_ratio * 3.4 + features.edge_strength * 0.8 + features.dark_ratio * 0.3)
        if label == "fight":
            return min(1.0, features.edge_strength * 1.9 + features.dark_ratio * 0.5)
        if label == "blood":
            return min(1.0, features.red_ratio * 3.2 + features.edge_strength * 0.4)
        if label in {"weapon_threat", "gunfire", "stabbing", "dead_body", "disturbing_image"}:
            return min(1.0, features.dark_ratio * 1.6 + features.edge_strength * 1.1)
        if label in {"explosion", "medical_injury"}:
            return min(1.0, features.edge_strength * 1.4 + features.red_ratio * 1.8 + features.bright_ratio * 0.5)
        if label in {"smoking_drugs", "marijuana", "tobacco"}:
            return min(1.0, features.gray_ratio * 1.7 + features.bright_ratio * 0.8)
        if label == "needle":
            return min(1.0, features.edge_strength * 1.3 + features.gray_ratio * 0.8 + features.dark_ratio * 0.4)
        if label in {"pill_abuse", "powder_drugs"}:
            return min(1.0, features.bright_ratio * 1.5 + features.pink_ratio * 0.6 + features.edge_strength * 0.4)
        if label in {"hard_drug_use", "drug_paraphernalia", "alcohol_abuse"}:
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
        step_ms = max(250, int(getattr(config, "scan_step_ms", 250)))
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
        enabled_detection_labels = set(
            getattr(config, "semantic_detection_labels", {}).get(
                self.category,
                list(prompt_bank),
            )
        )
        prompt_bank = {
            label: prompts
            for label, prompts in prompt_bank.items()
            if label in enabled_detection_labels
        }
        if not prompt_bank:
            return DetectorResult(
                category=self.category,
                source=self.source,
                status="done",
                detail=f"No {self.category} labels are enabled for detection.",
            )
        negative_prompts = SEMANTIC_NEGATIVE_PROMPTS[self.category]
        threshold = float(
            getattr(
                config,
                f"{self.category}_detection_threshold",
                SEMANTIC_LABEL_THRESHOLDS[self.category],
            )
        )
        gap_ms = max(
            1000,
            int(getattr(config, f"{self.category}_segment_gap_ms", getattr(config, "segment_gap_ms", 2000))),
        )
        min_hits = max(
            1,
            int(getattr(config, f"{self.category}_segment_min_hits", getattr(config, "segment_min_hits", 2))),
        )
        max_duration_ms = max(
            1000,
            int(getattr(config, f"{self.category}_segment_max_ms", getattr(config, "image_segment_max_ms", 15000))),
        )
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
            max_duration_ms=max_duration_ms,
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
