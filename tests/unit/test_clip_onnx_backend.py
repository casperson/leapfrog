"""Unit tests for the ONNX-backed semantic CLIP backend."""

from __future__ import annotations

import io
from unittest.mock import patch

import numpy as np
from PIL import Image

from leapfrog.detectors.clip_onnx import ClipOnnxSemanticBackend, get_shared_clip_backend


def _jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (8, 8), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


class FakeTokenizer:
    def __init__(self, token_map: dict[str, int]) -> None:
        self.token_map = token_map
        self.calls = 0

    def __call__(self, texts, padding=True, return_tensors="np"):
        self.calls += 1
        values = np.array([[self.token_map[text]] for text in texts], dtype=np.int64)
        return {"input_ids": values}


class FakeImageProcessor:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, images, return_tensors="np"):
        self.calls += 1
        sentinel = float(images.getpixel((0, 0))[0])
        return {"pixel_values": np.array([[[[sentinel]]]], dtype=np.float32)}


class FakeTextSession:
    def __init__(self, embedding_map: dict[int, np.ndarray]) -> None:
        self.embedding_map = embedding_map
        self.calls = 0

    def run(self, _, inputs):
        self.calls += 1
        ids = inputs["input_ids"].reshape(-1).tolist()
        embeddings = np.stack([self.embedding_map[int(token_id)] for token_id in ids], axis=0)
        return [embeddings.astype(np.float32)]


class FakeVisionSession:
    def __init__(self, embedding_map: dict[int, np.ndarray]) -> None:
        self.embedding_map = embedding_map
        self.calls = 0

    def run(self, _, inputs):
        self.calls += 1
        sentinel = int(float(inputs["pixel_values"][0, 0, 0, 0]))
        embedding = np.array([self.embedding_map[sentinel]], dtype=np.float32)
        return [embedding]


def _backend() -> ClipOnnxSemanticBackend:
    backend = ClipOnnxSemanticBackend(
        model_repo="repo/model",
        processor_repo="repo/processor",
        variant="int8",
    )
    backend._tokenizer = FakeTokenizer(
        {
            "fight prompt": 1,
            "blood prompt": 2,
            "neutral prompt": 3,
        }
    )
    backend._image_processor = FakeImageProcessor()
    backend._text_session = FakeTextSession(
        {
            1: np.array([1.0, 0.0], dtype=np.float32),
            2: np.array([0.0, 1.0], dtype=np.float32),
            3: np.array([0.0, -1.0], dtype=np.float32),
        }
    )
    backend._vision_session = FakeVisionSession(
        {
            254: np.array([1.0, 0.0], dtype=np.float32),
            255: np.array([1.0, 0.0], dtype=np.float32),
        }
    )
    return backend


def test_clip_backend_scores_prompts_and_caches_embeddings():
    backend = _backend()
    jpeg = _jpeg_bytes((255, 0, 0))
    prompt_bank = {
        "fight": ("fight prompt",),
        "blood": ("blood prompt",),
    }
    negative_prompts = ("neutral prompt",)

    first = backend.score_frame(jpeg, prompt_bank, negative_prompts)
    second = backend.score_frame(jpeg, prompt_bank, negative_prompts)

    assert first["fight"] > first["blood"]
    assert second == first
    assert backend._text_session.calls == 1
    assert backend._vision_session.calls == 1


def test_get_shared_clip_backend_reuses_instances():
    first = get_shared_clip_backend(
        model_repo="repo/model",
        processor_repo="repo/processor",
        variant="int8",
    )
    second = get_shared_clip_backend(
        model_repo="repo/model",
        processor_repo="repo/processor",
        variant="int8",
    )

    assert first is second


def test_clip_backend_prefers_local_assets_once_downloaded():
    backend = ClipOnnxSemanticBackend(
        model_repo="repo/model",
        processor_repo="repo/processor",
        variant="int8",
    )
    backend.model_dir.mkdir(parents=True, exist_ok=True)
    backend.processor_dir.mkdir(parents=True, exist_ok=True)
    (backend.model_dir / "onnx").mkdir(parents=True, exist_ok=True)
    vision_path = backend.model_dir / "onnx" / "vision_model_int8.onnx"
    text_path = backend.model_dir / "onnx" / "text_model_int8.onnx"
    vision_path.write_bytes(b"vision")
    text_path.write_bytes(b"text")
    for file_name in (
        "config.json",
        "merges.txt",
        "preprocessor_config.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
    ):
        (backend.processor_dir / file_name).write_text("{}", encoding="utf-8")

    fake_session = object()
    with patch("leapfrog.detectors.clip_onnx.hf_hub_download", side_effect=AssertionError("network not expected")), patch(
        "leapfrog.detectors.clip_onnx.snapshot_download",
        side_effect=AssertionError("network not expected"),
    ), patch(
        "leapfrog.detectors.clip_onnx.CLIPTokenizerFast.from_pretrained",
        return_value=FakeTokenizer({}),
    ), patch(
        "leapfrog.detectors.clip_onnx.CLIPImageProcessorImpl.from_pretrained",
        return_value=FakeImageProcessor(),
    ), patch(
        "leapfrog.detectors.clip_onnx.ort.InferenceSession",
        return_value=fake_session,
    ) as mock_session:
        backend.ensure_ready()

    assert backend._vision_session is fake_session
    assert backend._text_session is fake_session
    assert mock_session.call_count == 2
