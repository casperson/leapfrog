"""Common detector interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from ..domain import MediaScanTarget, MediaSegment


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
