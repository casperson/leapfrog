"""Unit tests for shared image detectors and clustering behavior."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

from leapfrog.detectors.image_common import FrameHit, cluster_frame_hits
from leapfrog.detectors.nudity import NudityDetector
from leapfrog.detectors.semantic import DrugsDetector, SexualContentDetector, ViolenceDetector
from leapfrog.domain import MediaScanTarget, SampledFrame


def _target() -> MediaScanTarget:
    return MediaScanTarget(
        media_id="guid-image",
        plex_guid="guid-image",
        title="Movie",
        file_path="/tmp/movie.mkv",
        rating_key="1",
    )


def _config(**kwargs):
    defaults = {
        "scan_step_ms": 250,
        "segment_gap_ms": 2000,
        "segment_min_hits": 2,
        "image_segment_max_ms": 15000,
        "confidence_threshold": 0.4,
        "scan_labels": ["FEMALE_BREAST_EXPOSED"],
        "nudenet_model": "640m",
        "nudenet_model_path": "",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _frames() -> list[SampledFrame]:
    return [
        SampledFrame(offset_ms=0, jpeg_bytes=b"frame-a"),
        SampledFrame(offset_ms=5000, jpeg_bytes=b"frame-b"),
        SampledFrame(offset_ms=10000, jpeg_bytes=b"frame-c"),
    ]


def _title_card_jpeg() -> bytes:
    image = Image.new("RGB", (320, 180), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 72, 240, 88), fill=(245, 245, 245))
    draw.rectangle((118, 98, 202, 108), fill=(245, 245, 245))
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


def _title_sequence_scene_jpeg() -> bytes:
    image = Image.new("RGB", (320, 180), (18, 18, 18))
    draw = ImageDraw.Draw(image)
    draw.rectangle((96, 42, 224, 150), fill=(198, 135, 112))
    draw.rectangle((130, 68, 190, 120), fill=(224, 158, 128))
    draw.rectangle((72, 18, 248, 31), fill=(245, 245, 245))
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


def test_cluster_frame_hits_caps_long_segments():
    hits = [
        FrameHit(offset_ms=0, confidence=0.9, labels=["fight"], jpeg_bytes=b"a"),
        FrameHit(offset_ms=1000, confidence=0.8, labels=["fight"], jpeg_bytes=b"b"),
        FrameHit(offset_ms=2000, confidence=0.7, labels=["fight"], jpeg_bytes=b"c"),
    ]

    segments = cluster_frame_hits(
        target=_target(),
        category="violence",
        source="semantic_clip",
        hits=hits,
        gap_ms=12000,
        min_hits=1,
        max_duration_ms=3000,
    )

    assert len(segments) == 1
    assert segments[0].end_ms == 3000


class FakeBackend:
    def __init__(self, scores_by_bytes: dict[bytes, dict[str, float]]) -> None:
        self._scores_by_bytes = scores_by_bytes

    def score_frame(
        self,
        jpeg_bytes: bytes,
        prompt_bank: dict[str, tuple[str, ...]],
        negative_prompts: tuple[str, ...] = (),
    ) -> dict[str, float]:
        scores = self._scores_by_bytes.get(jpeg_bytes, {})
        return {label: float(scores.get(label, 0.0)) for label in prompt_bank}


async def test_nudity_detector_scan_frames_still_clusters_hits():
    detector = NudityDetector()
    with patch(
        "leapfrog.detectors.nudity._classify_frame",
        side_effect=[
            (True, 0.9, ["FEMALE_BREAST_EXPOSED"]),
            (True, 0.8, ["FEMALE_BREAST_EXPOSED"]),
            (False, 0.0, []),
        ],
    ):
        result = await detector.scan_frames(
            _target(),
            _frames(),
            _config(segment_gap_ms=12000),
            progress_callback=AsyncMock(),
        )

    assert result.status == "done"
    assert len(result.segments) == 1
    assert result.segments[0].category == "nudity"
    assert result.segments[0].labels == "FEMALE_BREAST_EXPOSED"


async def test_sexual_content_detector_triggers_on_non_nude_prompt_hits():
    detector = SexualContentDetector(
        backend=FakeBackend(
            {
                b"frame-a": {"heavy_making_out": 0.91},
                b"frame-b": {"romantic_kiss": 0.87, "intimate_touch": 0.75},
            }
        )
    )

    result = await detector.scan_frames(
        _target(),
        _frames(),
        _config(
            segment_gap_ms=12000,
            semantic_detection_labels={
                "sexual_content": ["heavy_making_out", "romantic_kiss", "intimate_touch"],
            },
        ),
        progress_callback=AsyncMock(),
    )

    assert result.status == "done"
    assert len(result.segments) == 1
    assert result.segments[0].category == "sexual_content"
    assert result.segments[0].source == "semantic_clip"
    assert "heavy_making_out" in result.segments[0].labels
    assert "romantic_kiss" in result.segments[0].labels


async def test_violence_detector_triggers_on_violence_prompts_and_merges_labels():
    detector = ViolenceDetector(
        backend=FakeBackend(
            {
                b"frame-a": {"fight": 0.9},
                b"frame-b": {"weapon_threat": 0.82, "fight": 0.79},
            }
        )
    )

    result = await detector.scan_frames(
        _target(),
        _frames(),
        _config(segment_gap_ms=12000),
        progress_callback=AsyncMock(),
    )

    assert result.status == "done"
    assert len(result.segments) == 1
    assert result.segments[0].category == "violence"
    assert "fight" in result.segments[0].labels
    assert "weapon_threat" in result.segments[0].labels


async def test_drugs_detector_triggers_on_drug_prompts():
    detector = DrugsDetector(
        backend=FakeBackend(
            {
                b"frame-b": {"smoking_drugs": 0.88, "drug_paraphernalia": 0.8},
                b"frame-c": {"pill_abuse": 0.9},
            }
        )
    )

    result = await detector.scan_frames(
        _target(),
        _frames(),
        _config(segment_gap_ms=12000),
        progress_callback=AsyncMock(),
    )

    assert result.status == "done"
    assert len(result.segments) == 1
    assert result.segments[0].category == "drugs"
    assert "smoking_drugs" in result.segments[0].labels or "pill_abuse" in result.segments[0].labels


async def test_semantic_detector_suppresses_text_only_title_cards():
    title_card = _title_card_jpeg()
    detector = DrugsDetector(
        backend=FakeBackend(
            {
                title_card: {"marijuana": 0.96, "drug_paraphernalia": 0.91},
            }
        )
    )

    result = await detector.scan_frames(
        _target(),
        [
            SampledFrame(offset_ms=0, jpeg_bytes=title_card),
            SampledFrame(offset_ms=1000, jpeg_bytes=title_card),
        ],
        _config(segment_gap_ms=2000, segment_min_hits=1),
        progress_callback=AsyncMock(),
    )

    assert result.status == "done"
    assert result.segments == []
    assert "Suppressed 2 title-card-like frame(s)." in result.detail


async def test_semantic_detector_keeps_title_sequence_frames_with_scene_content():
    scene_frame = _title_sequence_scene_jpeg()
    detector = SexualContentDetector(
        backend=FakeBackend(
            {
                scene_frame: {"explicit_sex": 0.96},
            }
        )
    )

    result = await detector.scan_frames(
        _target(),
        [
            SampledFrame(offset_ms=0, jpeg_bytes=scene_frame),
            SampledFrame(offset_ms=1000, jpeg_bytes=scene_frame),
        ],
        _config(segment_gap_ms=2000, segment_min_hits=1),
        progress_callback=AsyncMock(),
    )

    assert result.status == "done"
    assert len(result.segments) == 1
    assert result.segments[0].category == "sexual_content"
    assert "Suppressed" not in result.detail
