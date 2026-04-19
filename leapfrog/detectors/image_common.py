"""Shared helpers for image-based detectors that operate on sampled frames."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import MediaScanTarget, MediaSegment
from ..paths import get_thumbnails_dir


@dataclass(slots=True)
class FrameHit:
    offset_ms: int
    confidence: float
    labels: list[str]
    jpeg_bytes: bytes


def cluster_frame_hits(
    *,
    target: MediaScanTarget,
    category: str,
    source: str,
    hits: list[FrameHit],
    gap_ms: int,
    min_hits: int,
) -> list[MediaSegment]:
    """Cluster adjacent detector hits into category segments with one thumbnail."""
    if not hits:
        return []

    thumbnails_dir = get_thumbnails_dir()
    thumbnails_dir.mkdir(parents=True, exist_ok=True)

    ordered_hits = sorted(hits, key=lambda hit: hit.offset_ms)
    segments: list[MediaSegment] = []
    cluster_hits: list[FrameHit] = [ordered_hits[0]]

    def flush() -> None:
        if len(cluster_hits) < max(1, min_hits):
            return
        best_hit = max(cluster_hits, key=lambda hit: hit.confidence)
        labels: list[str] = []
        for hit in cluster_hits:
            for label in hit.labels:
                if label not in labels:
                    labels.append(label)
        filename = f"{target.media_id.replace('/', '_')}_{category}_{cluster_hits[0].offset_ms}.jpg"
        full_path = thumbnails_dir / filename
        full_path.write_bytes(best_hit.jpeg_bytes)
        segments.append(
            MediaSegment(
                media_id=target.media_id,
                title=target.title,
                start_ms=cluster_hits[0].offset_ms,
                end_ms=cluster_hits[-1].offset_ms + gap_ms,
                category=category,
                source=source,
                confidence=best_hit.confidence,
                thumbnail_path=str(full_path),
                labels=",".join(labels),
            )
        )

    for hit in ordered_hits[1:]:
        if hit.offset_ms - cluster_hits[-1].offset_ms > gap_ms:
            flush()
            cluster_hits = [hit]
            continue
        cluster_hits.append(hit)

    flush()
    return segments
