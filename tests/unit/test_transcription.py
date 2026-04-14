"""Unit tests for Whisper fallback transcription helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leapfrog.transcription import WhisperTranscriber


async def test_whisper_transcriber_extracts_audio_before_transcribing(tmp_path):
    media_path = tmp_path / "movie.mkv"
    media_path.write_bytes(b"video")
    transcriber = WhisperTranscriber(model_name="base")

    with patch.object(
        transcriber,
        "_extract_audio",
        new=AsyncMock(return_value=str(tmp_path / "audio.wav")),
    ) as extract_audio, patch.object(
        transcriber,
        "_transcribe_blocking",
        return_value=[],
    ) as transcribe_blocking, patch(
        "leapfrog.transcription.asyncio.to_thread",
        new=AsyncMock(return_value=[]),
    ) as to_thread, patch(
        "leapfrog.transcription.os.unlink"
    ) as unlink:
        result = await transcriber.transcribe(str(media_path))

    assert result == []
    extract_audio.assert_awaited_once_with(str(media_path))
    to_thread.assert_awaited_once()
    assert to_thread.await_args.args == (
        transcribe_blocking,
        str(tmp_path / "audio.wav"),
    )
    unlink.assert_called_once_with(str(tmp_path / "audio.wav"))


async def test_extract_audio_raises_when_ffmpeg_fails(tmp_path):
    media_path = tmp_path / "movie.mkv"
    media_path.write_bytes(b"video")
    transcriber = WhisperTranscriber(model_name="base")

    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b"boom"))
    proc.returncode = 1

    with patch(
        "leapfrog.transcription.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        with pytest.raises(RuntimeError, match="ffmpeg audio extraction failed"):
            await transcriber._extract_audio(str(media_path))
