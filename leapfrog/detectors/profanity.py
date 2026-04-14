"""Subtitle-first profanity detector with optional Whisper fallback."""

from __future__ import annotations

import json
import re

from ..domain import DEFAULT_PROFANITY_TERMS, MediaScanTarget, MediaSegment
from ..logger import get_logger
from ..subtitles import SubtitleCue, extract_embedded_subtitles, load_external_subtitles
from ..transcription import WhisperTranscriber, WhisperUnavailableError
from .base import DetectorResult, ProgressCallback

logger = get_logger(__name__)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s']")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    lowered = text.lower()
    lowered = _NON_ALNUM_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", lowered).strip()


def _match_terms(text: str, terms: list[str]) -> list[str]:
    normalized_text = _normalize_text(text)
    tokens = set(normalized_text.split())
    matches: list[str] = []
    for term in terms:
        normalized_term = _normalize_text(term)
        if not normalized_term:
            continue
        if " " in normalized_term:
            if normalized_term in normalized_text:
                matches.append(term)
        elif normalized_term in tokens:
            matches.append(term)
    return matches


def _score_matches(matches: list[str]) -> float:
    if not matches:
        return 0.0
    return min(1.0, 0.5 + (0.25 * len(matches)))


def _merge_flagged_cues(
    target: MediaScanTarget,
    cues: list[tuple[SubtitleCue, list[str]]],
    *,
    source: str,
    merge_gap_ms: int,
) -> list[MediaSegment]:
    if not cues:
        return []

    merged: list[MediaSegment] = []
    current_start = cues[0][0].start_ms
    current_end = cues[0][0].end_ms
    current_matches = set(cues[0][1])
    excerpts = [cues[0][0].text]

    for cue, matches in cues[1:]:
        if cue.start_ms - current_end > merge_gap_ms:
            merged.append(
                MediaSegment(
                    media_id=target.media_id,
                    title=target.title,
                    start_ms=current_start,
                    end_ms=current_end,
                    category="profanity",
                    source=source,
                    confidence=_score_matches(sorted(current_matches)),
                    text_excerpt=" / ".join(excerpts)[:240],
                    labels=",".join(sorted(current_matches)),
                )
            )
            current_start = cue.start_ms
            current_end = cue.end_ms
            current_matches = set(matches)
            excerpts = [cue.text]
            continue
        current_end = max(current_end, cue.end_ms)
        current_matches.update(matches)
        excerpts.append(cue.text)

    merged.append(
        MediaSegment(
            media_id=target.media_id,
            title=target.title,
            start_ms=current_start,
            end_ms=current_end,
            category="profanity",
            source=source,
            confidence=_score_matches(sorted(current_matches)),
            text_excerpt=" / ".join(excerpts)[:240],
            labels=",".join(sorted(current_matches)),
        )
    )
    return merged


class ProfanityDetector:
    """Use subtitles first and fall back to Whisper transcription."""

    category = "profanity"

    def __init__(self, transcriber: WhisperTranscriber | None = None) -> None:
        self._transcriber = transcriber

    async def scan(
        self,
        target: MediaScanTarget,
        config,
        progress_callback: ProgressCallback | None = None,
    ) -> DetectorResult:
        """Analyze subtitles or transcript text for profanity."""
        term_list = self._load_terms(getattr(config, "profanity_terms", []))
        merge_gap_ms = int(getattr(config, "profanity_merge_gap_ms", 1500))

        cues = await load_external_subtitles(target.file_path)
        source = "subtitles"
        if not cues:
            cues = await extract_embedded_subtitles(target.file_path)
        if not cues and bool(getattr(config, "whisper_enabled", False)):
            source = "whisper"
            transcriber = self._transcriber or WhisperTranscriber(
                model_name=str(getattr(config, "whisper_model", "base"))
            )
            try:
                cues = await transcriber.transcribe(target.file_path)
            except WhisperUnavailableError as exc:
                logger.info("Whisper fallback unavailable for %s: %s", target.title, exc)
                return DetectorResult(
                    category=self.category,
                    source=source,
                    status="unavailable",
                    detail=str(exc),
                )
            except Exception as exc:
                logger.warning("Whisper fallback failed for %s: %s", target.title, exc)
                return DetectorResult(
                    category=self.category,
                    source=source,
                    status="failed",
                    detail=str(exc),
                )

        if not cues:
            return DetectorResult(
                category=self.category,
                source=source,
                status="done",
                detail="No usable subtitles or transcript were available.",
            )

        flagged: list[tuple[SubtitleCue, list[str]]] = []
        for cue in cues:
            matches = _match_terms(cue.text, term_list)
            if matches:
                flagged.append((cue, matches))

        segments = _merge_flagged_cues(target, flagged, source=source, merge_gap_ms=merge_gap_ms)
        if progress_callback:
            await progress_callback(1.0)
        return DetectorResult(
            category=self.category,
            source=source,
            status="done",
            segments=segments,
            detail=f"Scanned {len(cues)} caption/transcript lines.",
        )

    def _load_terms(self, raw_terms: list[str] | str) -> list[str]:
        if isinstance(raw_terms, list):
            return [str(item).strip() for item in raw_terms if str(item).strip()]
        if isinstance(raw_terms, str):
            try:
                parsed = json.loads(raw_terms)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except json.JSONDecodeError:
                pass
            return [
                line.strip()
                for line in raw_terms.splitlines()
                if line.strip()
            ]
        return DEFAULT_PROFANITY_TERMS.copy()
