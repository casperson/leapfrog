"""Runtime configuration backed by the SQLite settings table."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time

from . import database as db
from .domain import DEFAULT_DETECT_LABELS, DEFAULT_SKIP_LABELS


@dataclass
class Config:
    plex_url: str = ""
    plex_token: str = ""
    poll_interval: int = 5
    confidence_threshold: float = 0.4
    skip_buffer_ms: int = 1000
    scan_step_ms: int = 250
    scan_workers: int = 2
    nudenet_model: str = "640m"
    nudenet_model_path: str = ""
    semantic_model_repo: str = "Xenova/clip-vit-base-patch32"
    semantic_processor_repo: str = "openai/clip-vit-base-patch32"
    semantic_model_variant: str = "int8"
    semantic_detection_labels: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_DETECT_LABELS))
    default_skip_labels: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_SKIP_LABELS))
    segment_gap_ms: int = 2000
    segment_min_hits: int = 2
    image_segment_max_ms: int = 15000
    profanity_terms: list[str] = field(default_factory=list)
    profanity_allowlist: list[str] = field(default_factory=list)
    default_profanity_threshold: float = 0.5
    sexual_content_detection_threshold: float = 0.45
    violence_detection_threshold: float = 0.45
    drugs_detection_threshold: float = 0.45
    profanity_merge_gap_ms: int = 1500
    whisper_enabled: bool = True
    whisper_model: str = "base"
    log_buffer_capacity: int = 1000
    scan_ratings: list[str] = field(default_factory=list)  # empty = scan all ratings
    scan_labels: list[str] = field(default_factory=lambda: [
        "FEMALE_BREAST_EXPOSED",
        "FEMALE_GENITALIA_EXPOSED",
        "MALE_GENITALIA_EXPOSED",
        "ANUS_EXPOSED",
        "BUTTOCKS_EXPOSED",
    ])
    scan_window_start: time = field(default_factory=lambda: time(23, 0))
    scan_window_end: time = field(default_factory=lambda: time(6, 0))
    log_level: str = "INFO"

    @classmethod
    async def load(cls) -> "Config":
        s = await db.get_all_settings()

        def _time(val: str) -> time:
            h, m = val.split(":")
            return time(int(h), int(m))

        def _labels(val: str) -> list[str]:
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed if isinstance(x, str)]
            except Exception:
                pass
            # Return empty list if parsing fails - no scanning until labels are configured
            return []

        def _label_map(val: str, fallback: dict[str, list[str]]) -> dict[str, list[str]]:
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    result: dict[str, list[str]] = {}
                    for category, labels in parsed.items():
                        if isinstance(labels, list):
                            result[str(category)] = [str(label) for label in labels if isinstance(label, str)]
                    return {**fallback, **result}
            except Exception:
                pass
            return dict(fallback)

        return cls(
            plex_url=s.get("plex_url", ""),
            plex_token=s.get("plex_token", ""),
            poll_interval=int(s.get("poll_interval", "5")),
            confidence_threshold=float(s.get("confidence_threshold", "0.4")),
            skip_buffer_ms=int(s.get("skip_buffer_ms", "1000")),
            scan_step_ms=int(s.get("scan_step_ms", "250")),
            scan_workers=max(1, int(s.get("scan_workers", "2"))),
            nudenet_model=s.get("nudenet_model", "640m"),
            nudenet_model_path=s.get("nudenet_model_path", ""),
            semantic_model_repo=s.get("semantic_model_repo", "Xenova/clip-vit-base-patch32"),
            semantic_processor_repo=s.get("semantic_processor_repo", "openai/clip-vit-base-patch32"),
            semantic_model_variant=s.get("semantic_model_variant", "int8"),
            semantic_detection_labels=_label_map(
                s.get("semantic_detection_labels", json.dumps(DEFAULT_DETECT_LABELS)),
                DEFAULT_DETECT_LABELS,
            ),
            default_skip_labels=_label_map(
                s.get("default_skip_labels", json.dumps(DEFAULT_SKIP_LABELS)),
                DEFAULT_SKIP_LABELS,
            ),
            segment_gap_ms=int(s.get("segment_gap_ms", "2000")),
            segment_min_hits=int(s.get("segment_min_hits", "2")),
            image_segment_max_ms=int(s.get("image_segment_max_ms", "15000")),
            profanity_terms=_labels(s.get("profanity_terms", "[]")),
            profanity_allowlist=_labels(s.get("profanity_allowlist", "[]")),
            default_profanity_threshold=float(s.get("default_profanity_threshold", "0.5")),
            sexual_content_detection_threshold=float(s.get("sexual_content_detection_threshold", "0.45")),
            violence_detection_threshold=float(s.get("violence_detection_threshold", "0.45")),
            drugs_detection_threshold=float(s.get("drugs_detection_threshold", "0.45")),
            profanity_merge_gap_ms=int(s.get("profanity_merge_gap_ms", "1500")),
            whisper_enabled=s.get("whisper_enabled", "1") == "1",
            whisper_model=s.get("whisper_model", "base"),
            log_buffer_capacity=max(1, int(s.get("log_buffer_capacity", "1000"))),
            scan_ratings=_labels(s.get("scan_ratings", "[]")),
            scan_labels=_labels(s.get("scan_labels", "[]")),
            scan_window_start=_time(s.get("scan_window_start", "23:00")),
            scan_window_end=_time(s.get("scan_window_end", "06:00")),
            log_level=s.get("log_level", "INFO"),
        )

    def is_configured(self) -> bool:
        return bool(self.plex_url and self.plex_token)

    def is_scan_window(self) -> bool:
        """Return True if current local time is within the scan window."""
        from datetime import datetime
        now = datetime.now().time().replace(second=0, microsecond=0)
        start = self.scan_window_start
        end = self.scan_window_end
        if start <= end:
            return start <= now <= end
        # Window wraps midnight (e.g. 23:00 – 06:00)
        return now >= start or now <= end
