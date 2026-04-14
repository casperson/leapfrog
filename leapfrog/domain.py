"""Shared domain models for segments, scan targets, and status records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUPPORTED_CATEGORIES = ("nudity", "profanity")
PREFERENCE_CATEGORIES = ("nudity", "profanity", "violence", "drugs")
DEFAULT_PROFANITY_TERMS = [
    "asshole",
    "bastard",
    "bitch",
    "bullshit",
    "dammit",
    "damn",
    "dick",
    "fucker",
    "fucking",
    "goddamn",
    "hell",
    "motherfucker",
    "pissed off",
    "shit",
    "son of a bitch",
]
DEFAULT_CATEGORY_THRESHOLDS = {
    "nudity": 0.6,
    "profanity": 0.5,
    "violence": 0.5,
    "drugs": 0.5,
}


@dataclass(slots=True)
class MediaScanTarget:
    media_id: str
    plex_guid: str
    title: str
    file_path: str
    rating_key: str
    force_scan: bool = False


@dataclass(slots=True, init=False)
class Segment:
    media_id: str
    start_time: float
    end_time: float
    category: str
    source: str
    confidence: float | None
    text_excerpt: str | None
    title: str
    thumbnail_path: str | None
    labels: str
    review_status: str
    created_at: str | None
    updated_at: str | None

    def __init__(
        self,
        media_id: str,
        start_time: float | None = None,
        end_time: float | None = None,
        category: str = "nudity",
        source: str = "nudenet",
        confidence: float | None = None,
        text_excerpt: str | None = None,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        title: str = "",
        thumbnail_path: str | None = None,
        labels: str = "",
        review_status: str = "pending",
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        if start_time is None:
            start_time = 0.0 if start_ms is None else start_ms / 1000
        if end_time is None:
            end_time = 0.0 if end_ms is None else end_ms / 1000

        self.media_id = media_id
        self.start_time = float(start_time)
        self.end_time = float(end_time)
        self.category = category
        self.source = source
        self.confidence = confidence
        self.text_excerpt = text_excerpt
        self.title = title
        self.thumbnail_path = thumbnail_path
        self.labels = labels
        self.review_status = review_status
        self.created_at = created_at
        self.updated_at = updated_at

    @property
    def start_ms(self) -> int:
        return int(round(self.start_time * 1000))

    @property
    def end_ms(self) -> int:
        return int(round(self.end_time * 1000))

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Segment":
        media_id = str(row.get("media_id") or row.get("plex_guid") or "")
        return cls(
            media_id=media_id,
            start_ms=int(row.get("start_ms") or 0),
            end_ms=int(row.get("end_ms") or 0),
            category=str(row.get("category") or "nudity"),
            source=str(row.get("source") or "nudenet"),
            confidence=row.get("confidence"),
            text_excerpt=row.get("text_excerpt"),
            title=str(row.get("title") or ""),
            thumbnail_path=row.get("thumbnail_path"),
            labels=str(row.get("labels") or ""),
            review_status=str(row.get("review_status") or "pending"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    def to_record(self, *, plex_guid: str | None = None) -> dict[str, Any]:
        record = {
            "media_id": self.media_id,
            "plex_guid": plex_guid or self.media_id,
            "title": self.title,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "category": self.category,
            "source": self.source,
            "confidence": self.confidence,
            "text_excerpt": self.text_excerpt,
            "thumbnail_path": self.thumbnail_path,
            "labels": self.labels,
            "review_status": self.review_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return record


MediaSegment = Segment


@dataclass(slots=True)
class UserCategoryPreference:
    user_id: str
    category: str
    enabled: bool
    threshold: float


@dataclass(slots=True)
class ScanStatusRecord:
    media_id: str
    category: str
    status: str
    source: str = ""
    detail: str = ""
