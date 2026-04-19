"""Unit tests for subtitle-first profanity detection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from leapfrog.detectors.profanity import ProfanityDetector
from leapfrog.domain import MediaScanTarget
from leapfrog.subtitles import SubtitleCue
from leapfrog.transcription import WhisperUnavailableError


def _target() -> MediaScanTarget:
    return MediaScanTarget(
        media_id="guid-1",
        plex_guid="guid-1",
        title="Movie",
        file_path="/tmp/movie.mkv",
        rating_key="1",
    )


def _config(**kwargs):
    defaults = {
        "profanity_terms": ["shit", "bad phrase"],
        "profanity_allowlist": ["shitake"],
        "profanity_merge_gap_ms": 1500,
        "whisper_enabled": True,
        "whisper_model": "base",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


async def test_profanity_detector_uses_subtitles_first():
    detector = ProfanityDetector()
    cues = [
        SubtitleCue(start_ms=1000, end_ms=2500, text="This is shit."),
        SubtitleCue(start_ms=4000, end_ms=5000, text="Completely safe"),
    ]
    with patch(
        "leapfrog.detectors.profanity.load_external_subtitles",
        new=AsyncMock(return_value=cues),
    ), patch(
        "leapfrog.detectors.profanity.extract_embedded_subtitles",
        new=AsyncMock(return_value=[]),
    ):
        result = await detector.scan(_target(), _config())

    assert result.status == "done"
    assert result.source == "subtitles"
    assert len(result.segments) == 1
    assert result.segments[0].category == "profanity"
    assert result.segments[0].source == "subtitles"
    assert result.segments[0].confidence is not None
    assert "shit" in (result.segments[0].text_excerpt or "").lower()


async def test_profanity_detector_uses_embedded_subtitles_before_whisper():
    transcriber = AsyncMock()
    detector = ProfanityDetector(transcriber=transcriber)
    cues = [SubtitleCue(start_ms=1000, end_ms=2000, text="shit happens")]
    with patch(
        "leapfrog.detectors.profanity.load_external_subtitles",
        new=AsyncMock(return_value=[]),
    ), patch(
        "leapfrog.detectors.profanity.extract_embedded_subtitles",
        new=AsyncMock(return_value=cues),
    ):
        result = await detector.scan(_target(), _config())

    assert result.status == "done"
    assert result.source == "subtitles"
    assert len(result.segments) == 1
    transcriber.transcribe.assert_not_awaited()


async def test_profanity_detector_uses_whisper_when_subtitles_missing():
    transcriber = AsyncMock()
    transcriber.transcribe.return_value = [
        SubtitleCue(start_ms=2000, end_ms=3200, text="A bad phrase appears"),
    ]
    detector = ProfanityDetector(transcriber=transcriber)
    with patch(
        "leapfrog.detectors.profanity.load_external_subtitles",
        new=AsyncMock(return_value=[]),
    ), patch(
        "leapfrog.detectors.profanity.extract_embedded_subtitles",
        new=AsyncMock(return_value=[]),
    ):
        result = await detector.scan(_target(), _config())

    assert result.status == "done"
    assert result.source == "whisper"
    assert len(result.segments) == 1
    transcriber.transcribe.assert_awaited_once()


async def test_profanity_detector_returns_no_segments_when_text_is_clean():
    detector = ProfanityDetector()
    cues = [
        SubtitleCue(start_ms=1000, end_ms=2500, text="This is perfectly clean."),
        SubtitleCue(start_ms=4000, end_ms=5000, text="Nothing offensive here."),
    ]
    with patch(
        "leapfrog.detectors.profanity.load_external_subtitles",
        new=AsyncMock(return_value=cues),
    ), patch(
        "leapfrog.detectors.profanity.extract_embedded_subtitles",
        new=AsyncMock(return_value=[]),
    ):
        result = await detector.scan(_target(), _config())

    assert result.status == "done"
    assert result.source == "subtitles"
    assert result.segments == []


async def test_profanity_detector_marks_whisper_unavailable():
    transcriber = AsyncMock()
    transcriber.transcribe.side_effect = WhisperUnavailableError("whisper missing")
    detector = ProfanityDetector(transcriber=transcriber)
    with patch(
        "leapfrog.detectors.profanity.load_external_subtitles",
        new=AsyncMock(return_value=[]),
    ), patch(
        "leapfrog.detectors.profanity.extract_embedded_subtitles",
        new=AsyncMock(return_value=[]),
    ):
        result = await detector.scan(_target(), _config())

    assert result.status == "unavailable"
    assert result.source == "whisper"
    assert "whisper missing" in result.detail


async def test_profanity_detector_matches_root_term_derivations():
    detector = ProfanityDetector()
    cues = [SubtitleCue(start_ms=1000, end_ms=2000, text="That motherfucker ran away.")]
    with patch(
        "leapfrog.detectors.profanity.load_external_subtitles",
        new=AsyncMock(return_value=cues),
    ), patch(
        "leapfrog.detectors.profanity.extract_embedded_subtitles",
        new=AsyncMock(return_value=[]),
    ):
        result = await detector.scan(_target(), _config(profanity_terms=["fuck"]))

    assert result.status == "done"
    assert len(result.segments) == 1
    assert result.segments[0].labels == "fuck"


async def test_profanity_detector_allowlist_reduces_false_positives():
    detector = ProfanityDetector()
    cues = [SubtitleCue(start_ms=1000, end_ms=2000, text="We cooked a shitake mushroom soup.")]
    with patch(
        "leapfrog.detectors.profanity.load_external_subtitles",
        new=AsyncMock(return_value=cues),
    ), patch(
        "leapfrog.detectors.profanity.extract_embedded_subtitles",
        new=AsyncMock(return_value=[]),
    ):
        result = await detector.scan(
            _target(),
            _config(profanity_terms=["shit"], profanity_allowlist=["shitake"]),
        )

    assert result.status == "done"
    assert result.segments == []
