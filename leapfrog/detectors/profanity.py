"""Subtitle-first profanity detector with stronger root and phrase matching."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..domain import (
    DEFAULT_PROFANITY_ALLOWLIST,
    DEFAULT_PROFANITY_TERMS,
    MediaScanTarget,
    MediaSegment,
)
from ..logger import get_logger
from ..subtitles import SubtitleCue, extract_embedded_subtitles, load_external_subtitles
from ..transcription import WhisperTranscriber, WhisperUnavailableError
from .base import DetectorResult, ProgressCallback

logger = get_logger(__name__)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s']")
_WHITESPACE_RE = re.compile(r"\s+")
_ROOT_FALSE_POSITIVE_TOKENS: dict[str, set[str]] = {
    "hell": {
        "hello",
        "hellos",
        "shell",
        "shells",
        "shelling",
        "shelled",
        "hellenic",
        "hellenistic",
    },
}


@dataclass(frozen=True, slots=True)
class ProfanityRule:
    raw: str
    normalized: str
    is_phrase: bool
    is_root: bool


def _normalize_text(text: str) -> str:
    lowered = text.lower()
    lowered = _NON_ALNUM_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", lowered).strip()


def _tokenize(text: str) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    return [token for token in normalized.split(" ") if token]


def _build_rules(terms: list[str]) -> list[ProfanityRule]:
    rules: list[ProfanityRule] = []
    seen: set[str] = set()
    for raw_term in terms:
        normalized = _normalize_text(raw_term)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        is_phrase = " " in normalized
        rules.append(
            ProfanityRule(
                raw=raw_term,
                normalized=normalized,
                is_phrase=is_phrase,
                is_root=not is_phrase,
            )
        )
    return rules


def _token_allowed(token: str, rule: ProfanityRule, allowlist: set[str]) -> bool:
    return token in allowlist or token in _ROOT_FALSE_POSITIVE_TOKENS.get(rule.normalized, set())


def _match_rules(
    text: str,
    rules: list[ProfanityRule],
    *,
    allowlist: set[str],
) -> list[str]:
    normalized_text = _normalize_text(text)
    if not normalized_text:
        return []
    tokens = _tokenize(normalized_text)
    matches: list[str] = []
    for rule in rules:
        if rule.is_phrase:
            if rule.normalized in normalized_text:
                matches.append(rule.raw)
            continue
        matched = False
        for token in tokens:
            if _token_allowed(token, rule, allowlist):
                continue
            if rule.normalized == token:
                matched = True
                break
            if rule.is_root and rule.normalized in token:
                matched = True
                break
        if matched:
            matches.append(rule.raw)
    return matches


def _score_matches(matches: list[str]) -> float:
    if not matches:
        return 0.0
    return min(1.0, 0.45 + (0.18 * len(set(matches))))


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
        rules = _build_rules(self._load_terms(getattr(config, "profanity_terms", [])))
        allowlist = {
            _normalize_text(value)
            for value in self._load_allowlist(getattr(config, "profanity_allowlist", []))
            if _normalize_text(value)
        }
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
            matches = _match_rules(cue.text, rules, allowlist=allowlist)
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
            values = [str(item).strip() for item in raw_terms if str(item).strip()]
            return values or DEFAULT_PROFANITY_TERMS.copy()
        if isinstance(raw_terms, str):
            try:
                parsed = json.loads(raw_terms)
                if isinstance(parsed, list):
                    values = [str(item).strip() for item in parsed if str(item).strip()]
                    return values or DEFAULT_PROFANITY_TERMS.copy()
            except json.JSONDecodeError:
                pass
            values = [
                line.strip()
                for line in raw_terms.splitlines()
                if line.strip()
            ]
            return values or DEFAULT_PROFANITY_TERMS.copy()
        return DEFAULT_PROFANITY_TERMS.copy()

    def _load_allowlist(self, raw_allowlist: list[str] | str) -> list[str]:
        if isinstance(raw_allowlist, list):
            values = [str(item).strip() for item in raw_allowlist if str(item).strip()]
            return values or DEFAULT_PROFANITY_ALLOWLIST.copy()
        if isinstance(raw_allowlist, str):
            try:
                parsed = json.loads(raw_allowlist)
                if isinstance(parsed, list):
                    values = [str(item).strip() for item in parsed if str(item).strip()]
                    return values or DEFAULT_PROFANITY_ALLOWLIST.copy()
            except json.JSONDecodeError:
                pass
            values = [
                line.strip()
                for line in raw_allowlist.splitlines()
                if line.strip()
            ]
            return values or DEFAULT_PROFANITY_ALLOWLIST.copy()
        return DEFAULT_PROFANITY_ALLOWLIST.copy()
