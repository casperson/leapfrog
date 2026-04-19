"""Common detector interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from ..domain import MediaScanTarget, MediaSegment, SampledFrame


ProgressCallback = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class DetectorResult:
    category: str
    source: str
    status: str
    segments: list[MediaSegment] = field(default_factory=list)
    detail: str = ""


class MediaDetector(Protocol):
    """Protocol each media detector must implement."""

    category: str

    async def scan(
        self,
        target: MediaScanTarget,
        config,
        progress_callback: ProgressCallback | None = None,
    ) -> DetectorResult:
        """Analyze a media target and return category-specific segments."""


class FrameMediaDetector(MediaDetector, Protocol):
    """Protocol for image detectors that consume a shared sampled-frame list."""

    async def scan_frames(
        self,
        target: MediaScanTarget,
        frames: list[SampledFrame],
        config,
        progress_callback: ProgressCallback | None = None,
    ) -> DetectorResult:
        """Analyze shared sampled frames and return category-specific segments."""
