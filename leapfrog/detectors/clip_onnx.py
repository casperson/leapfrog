"""Local ONNX-backed CLIP scoring for semantic image detectors."""

from __future__ import annotations

import hashlib
import io
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image
from huggingface_hub import hf_hub_download, snapshot_download

# Keep Transformers on the tokenization / preprocessing path only.
os.environ.setdefault("USE_TORCH", "0")
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

from transformers import CLIPTokenizerFast

try:
    from transformers import CLIPImageProcessorPil as CLIPImageProcessorImpl
except ImportError:  # pragma: no cover - older transformers fallback.
    from transformers import CLIPImageProcessor as CLIPImageProcessorImpl

from ..logger import get_logger
from ..paths import get_models_dir

logger = get_logger(__name__)

SEMANTIC_MODEL_VARIANTS: dict[str, tuple[str, str]] = {
    "fp32": ("onnx/vision_model.onnx", "onnx/text_model.onnx"),
    "fp16": ("onnx/vision_model_fp16.onnx", "onnx/text_model_fp16.onnx"),
    "int8": ("onnx/vision_model_int8.onnx", "onnx/text_model_int8.onnx"),
    "uint8": ("onnx/vision_model_uint8.onnx", "onnx/text_model_uint8.onnx"),
}

PROCESSOR_FILES = (
    "config.json",
    "merges.txt",
    "preprocessor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
)

_BACKEND_LOCK = threading.Lock()
_BACKENDS: dict[tuple[str, str, str], "ClipOnnxSemanticBackend"] = {}


class SemanticModelUnavailableError(RuntimeError):
    """Raised when the semantic ONNX model cannot be prepared locally."""


@dataclass(slots=True)
class PromptEmbeddingBundle:
    """Normalized prompt embeddings grouped by label plus shared negatives."""

    label_embeddings: dict[str, np.ndarray]
    negative_embeddings: np.ndarray


class ClipOnnxSemanticBackend:
    """Local ONNX CLIP backend with cached prompt and frame embeddings."""

    def __init__(
        self,
        *,
        model_repo: str,
        processor_repo: str,
        variant: str,
        image_cache_size: int = 256,
    ) -> None:
        self.model_repo = model_repo
        self.processor_repo = processor_repo
        self.variant = variant if variant in SEMANTIC_MODEL_VARIANTS else "int8"
        self.image_cache_size = max(16, image_cache_size)
        self._runtime_lock = threading.RLock()
        self._prompt_cache: dict[tuple[tuple[tuple[str, tuple[str, ...]], ...], tuple[str, ...]], PromptEmbeddingBundle] = {}
        self._image_cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._tokenizer = None
        self._image_processor = None
        self._vision_session: ort.InferenceSession | None = None
        self._text_session: ort.InferenceSession | None = None

    @property
    def model_dir(self) -> Path:
        return get_models_dir() / f"semantic_{self.model_repo.replace('/', '_')}"

    @property
    def processor_dir(self) -> Path:
        return self.model_dir / f"processor_{self.processor_repo.replace('/', '_')}"

    def _resolve_local_repo_file(self, base_dir: Path, relative_path: str) -> Path | None:
        """Return an already-downloaded repo file path when present locally."""
        candidate = base_dir / relative_path
        if candidate.is_file():
            return candidate
        return None

    def ensure_ready(self) -> None:
        """Download model assets if needed and initialize ONNX sessions."""
        with self._runtime_lock:
            if self._vision_session is not None and self._text_session is not None:
                return

            vision_relpath, text_relpath = SEMANTIC_MODEL_VARIANTS[self.variant]
            self.model_dir.mkdir(parents=True, exist_ok=True)
            self.processor_dir.mkdir(parents=True, exist_ok=True)

            try:
                vision_path = self._resolve_local_repo_file(self.model_dir, vision_relpath) or Path(
                    hf_hub_download(
                        repo_id=self.model_repo,
                        filename=vision_relpath,
                        local_dir=str(self.model_dir),
                    )
                )
                text_path = self._resolve_local_repo_file(self.model_dir, text_relpath) or Path(
                    hf_hub_download(
                        repo_id=self.model_repo,
                        filename=text_relpath,
                        local_dir=str(self.model_dir),
                    )
                )
                missing_processor_files = [
                    file_name
                    for file_name in PROCESSOR_FILES
                    if not (self.processor_dir / file_name).is_file()
                ]
                if missing_processor_files:
                    snapshot_download(
                        repo_id=self.processor_repo,
                        allow_patterns=list(PROCESSOR_FILES),
                        local_dir=str(self.processor_dir),
                    )
            except Exception as exc:
                raise SemanticModelUnavailableError(
                    f"Could not prepare semantic model assets locally: {exc}"
                ) from exc

            try:
                self._tokenizer = CLIPTokenizerFast.from_pretrained(
                    str(self.processor_dir),
                    local_files_only=True,
                )
                self._image_processor = CLIPImageProcessorImpl.from_pretrained(
                    str(self.processor_dir),
                    local_files_only=True,
                )
                providers = ["CPUExecutionProvider"]
                self._vision_session = ort.InferenceSession(str(vision_path), providers=providers)
                self._text_session = ort.InferenceSession(str(text_path), providers=providers)
            except Exception as exc:
                raise SemanticModelUnavailableError(
                    f"Semantic ONNX runtime could not be initialized: {exc}"
                ) from exc

            logger.info(
                "Semantic CLIP backend ready from %s (%s)",
                self.model_repo,
                self.variant,
            )

    def score_frame(
        self,
        jpeg_bytes: bytes,
        prompt_bank: dict[str, tuple[str, ...]],
        negative_prompts: tuple[str, ...] = (),
    ) -> dict[str, float]:
        """Return per-label zero-shot scores for one frame."""
        self.ensure_ready()
        image_embedding = self._get_image_embedding(jpeg_bytes)
        prompt_bundle = self._get_prompt_bundle(prompt_bank, negative_prompts)
        scores: dict[str, float] = {}

        for label, label_embeddings in prompt_bundle.label_embeddings.items():
            positive_logits = (label_embeddings @ image_embedding).astype(np.float32)
            if prompt_bundle.negative_embeddings.size == 0:
                scores[label] = float((float(np.max(positive_logits)) + 1.0) / 2.0)
                continue

            negative_logits = (prompt_bundle.negative_embeddings @ image_embedding).astype(np.float32)
            logits = np.concatenate((positive_logits, negative_logits), axis=0) * 100.0
            probs = self._softmax(logits)
            scores[label] = float(np.max(probs[: positive_logits.shape[0]]))

        return scores

    def _get_image_embedding(self, jpeg_bytes: bytes) -> np.ndarray:
        key = hashlib.sha1(jpeg_bytes).hexdigest()

        with self._runtime_lock:
            cached = self._image_cache.get(key)
            if cached is not None:
                self._image_cache.move_to_end(key)
                return cached

        image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        inputs = self._image_processor(images=image, return_tensors="np")
        outputs = self._vision_session.run(  # type: ignore[union-attr]
            None,
            {"pixel_values": inputs["pixel_values"].astype(np.float32)},
        )[0]
        embedding = self._normalize(outputs.astype(np.float32)[0])

        with self._runtime_lock:
            self._image_cache[key] = embedding
            self._image_cache.move_to_end(key)
            while len(self._image_cache) > self.image_cache_size:
                self._image_cache.popitem(last=False)

        return embedding

    def _get_prompt_bundle(
        self,
        prompt_bank: dict[str, tuple[str, ...]],
        negative_prompts: tuple[str, ...],
    ) -> PromptEmbeddingBundle:
        cache_key = (
            tuple((label, tuple(prompts)) for label, prompts in sorted(prompt_bank.items())),
            tuple(negative_prompts),
        )
        with self._runtime_lock:
            cached = self._prompt_cache.get(cache_key)
            if cached is not None:
                return cached

        ordered_specs: list[tuple[str, int]] = []
        texts: list[str] = []
        for label, prompts in cache_key[0]:
            ordered_specs.append((label, len(prompts)))
            texts.extend(prompts)
        negative_count = len(negative_prompts)
        texts.extend(negative_prompts)
        embeddings = self._encode_texts(texts)

        label_embeddings: dict[str, np.ndarray] = {}
        offset = 0
        for label, count in ordered_specs:
            label_embeddings[label] = embeddings[offset : offset + count]
            offset += count
        negative_embeddings = embeddings[offset : offset + negative_count]

        bundle = PromptEmbeddingBundle(
            label_embeddings=label_embeddings,
            negative_embeddings=negative_embeddings,
        )
        with self._runtime_lock:
            self._prompt_cache[cache_key] = bundle
        return bundle

    def _encode_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, 512), dtype=np.float32)

        inputs = self._tokenizer(  # type: ignore[operator]
            texts,
            padding=True,
            return_tensors="np",
        )
        outputs = self._text_session.run(  # type: ignore[union-attr]
            None,
            {"input_ids": inputs["input_ids"].astype(np.int64)},
        )[0]
        return self._normalize(outputs.astype(np.float32))

    @staticmethod
    def _normalize(values: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(values, axis=-1, keepdims=True)
        norms = np.maximum(norms, 1e-12)
        return values / norms

    @staticmethod
    def _softmax(values: np.ndarray) -> np.ndarray:
        shifted = values - np.max(values)
        exp = np.exp(shifted)
        return exp / np.maximum(np.sum(exp), 1e-12)


def get_shared_clip_backend(
    *,
    model_repo: str,
    processor_repo: str,
    variant: str,
) -> ClipOnnxSemanticBackend:
    """Return a shared backend instance for the configured CLIP assets."""
    key = (model_repo, processor_repo, variant if variant in SEMANTIC_MODEL_VARIANTS else "int8")
    with _BACKEND_LOCK:
        backend = _BACKENDS.get(key)
        if backend is None:
            backend = ClipOnnxSemanticBackend(
                model_repo=model_repo,
                processor_repo=processor_repo,
                variant=key[2],
            )
            _BACKENDS[key] = backend
        return backend
