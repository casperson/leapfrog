"""Unit tests for shared image detectors and clustering behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        "segment_gap_ms": 12000,
        "segment_min_hits": 1,
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
        result = await detector.scan_frames(_target(), _frames(), _config(), progress_callback=AsyncMock())

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

    result = await detector.scan_frames(_target(), _frames(), _config(), progress_callback=AsyncMock())

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

    result = await detector.scan_frames(_target(), _frames(), _config(), progress_callback=AsyncMock())

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

    result = await detector.scan_frames(_target(), _frames(), _config(), progress_callback=AsyncMock())

    assert result.status == "done"
    assert len(result.segments) == 1
    assert result.segments[0].category == "drugs"
    assert "smoking_drugs" in result.segments[0].labels or "pill_abuse" in result.segments[0].labels
