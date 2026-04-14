"""Subtitle discovery, extraction, and parsing helpers."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from .frame_extractor import _FFMPEG_BIN
from .logger import get_logger

logger = get_logger(__name__)

_TIMECODE_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ASS_TAG_RE = re.compile(r"\{[^}]+\}")
_WHITESPACE_RE = re.compile(r"\s+")
_SUPPORTED_EXTENSIONS = (".srt", ".vtt")


@dataclass(slots=True)
class SubtitleCue:
    start_ms: int
    end_ms: int
    text: str


def _parse_timecode(value: str) -> int:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    sec, millis = seconds.split(".")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(sec) * 1000
        + int(millis)
    )


def clean_caption_text(text: str) -> str:
    """Normalize a subtitle line for matching and UI display."""
    cleaned = _HTML_TAG_RE.sub(" ", text)
    cleaned = _ASS_TAG_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("\\N", " ")
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def parse_subtitle_text(raw_text: str) -> list[SubtitleCue]:
    """Parse SRT or WebVTT subtitle text into timestamped cues."""
    cues: list[SubtitleCue] = []
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if not line or line == "WEBVTT" or line.startswith("NOTE"):
            idx += 1
            continue
        if line.isdigit():
            idx += 1
            if idx >= len(lines):
                break
            line = lines[idx].strip()
        match = _TIMECODE_RE.match(line)
        if not match:
            idx += 1
            continue
        start_ms = _parse_timecode(match.group("start"))
        end_ms = _parse_timecode(match.group("end"))
        idx += 1
        payload: list[str] = []
        while idx < len(lines) and lines[idx].strip():
            payload.append(lines[idx].strip())
            idx += 1
        text = clean_caption_text(" ".join(payload))
        if text:
            cues.append(SubtitleCue(start_ms=start_ms, end_ms=end_ms, text=text))
        idx += 1
    return cues


def find_external_subtitle_files(file_path: str) -> list[Path]:
    """Return sibling subtitle files that look like they belong to the media file."""
    media_path = Path(file_path)
    parent = media_path.parent
    stem = media_path.stem
    matches: list[Path] = []
    for ext in _SUPPORTED_EXTENSIONS:
        matches.extend(sorted(parent.glob(f"{stem}*{ext}")))
    return [candidate for candidate in matches if candidate.is_file()]


async def load_external_subtitles(file_path: str) -> list[SubtitleCue]:
    """Load cues from the first usable sibling subtitle file."""
    for candidate in find_external_subtitle_files(file_path):
        try:
            raw_text = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw_text = candidate.read_text(encoding="utf-8-sig", errors="ignore")
        cues = parse_subtitle_text(raw_text)
        if cues:
            logger.info("Loaded %d subtitle cue(s) from %s", len(cues), candidate)
            return cues
    return []


async def extract_embedded_subtitles(file_path: str) -> list[SubtitleCue]:
    """Extract the first subtitle stream to SRT via ffmpeg and parse it."""
    cmd = [
        _FFMPEG_BIN,
        "-loglevel",
        "error",
        "-i",
        file_path,
        "-map",
        "0:s:0",
        "-f",
        "srt",
        "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        logger.warning("Timed out extracting embedded subtitles for %s", file_path)
        return []
    if proc.returncode != 0 or not stdout:
        if stderr:
            logger.debug(
                "Embedded subtitle extraction failed for %s: %s",
                file_path,
                stderr.decode(errors="replace")[:200],
            )
        return []
    return parse_subtitle_text(stdout.decode("utf-8", errors="ignore"))
