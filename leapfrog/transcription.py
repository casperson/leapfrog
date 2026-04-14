"""Optional Whisper transcription support for profanity fallback scans."""

from __future__ import annotations

import asyncio
import os
import tempfile

from .frame_extractor import _FFMPEG_BIN
from .logger import get_logger
from .subtitles import SubtitleCue, clean_caption_text

logger = get_logger(__name__)


class WhisperUnavailableError(RuntimeError):
    """Raised when the optional Whisper dependency is unavailable."""


class WhisperTranscriber:
    """Thin wrapper around the optional local Whisper package."""

    def __init__(self, model_name: str = "base") -> None:
        self.model_name = model_name

    async def transcribe(self, file_path: str) -> list[SubtitleCue]:
        """Return transcript segments with millisecond timing."""
        audio_path = await self._extract_audio(file_path)
        try:
            return await asyncio.to_thread(self._transcribe_blocking, audio_path)
        finally:
            try:
                os.unlink(audio_path)
            except FileNotFoundError:
                pass

    async def _extract_audio(self, file_path: str) -> str:
        """Extract mono 16 kHz WAV audio for Whisper ingestion."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            output_path = handle.name
        cmd = [
            _FFMPEG_BIN,
            "-loglevel",
            "error",
            "-i",
            file_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            output_path,
            "-y",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            try:
                os.unlink(output_path)
            except FileNotFoundError:
                pass
            message = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            raise RuntimeError(
                f"ffmpeg audio extraction failed for Whisper fallback: {message[:200]}"
            )
        return output_path

    def _transcribe_blocking(self, file_path: str) -> list[SubtitleCue]:
        try:
            import whisper
        except ImportError as exc:  # pragma: no cover - exercised through wiring tests
            raise WhisperUnavailableError(
                "Install the optional 'whisper' package to enable audio fallback."
            ) from exc

        model = whisper.load_model(self.model_name)
        result = model.transcribe(file_path, verbose=False)
        segments: list[SubtitleCue] = []
        for item in result.get("segments", []):
            text = clean_caption_text(str(item.get("text") or ""))
            if not text:
                continue
            segments.append(
                SubtitleCue(
                    start_ms=int(float(item.get("start", 0.0)) * 1000),
                    end_ms=int(float(item.get("end", 0.0)) * 1000),
                    text=text,
                )
            )
        logger.info("Transcribed %d Whisper segment(s) for %s", len(segments), file_path)
        return segments
