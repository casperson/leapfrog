"""Shared domain models for segments, scan targets, and status records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .vidangel_taxonomy import get_vidangel_category_rows, get_vidangel_leaf_lookup


@dataclass(frozen=True, slots=True)
class CategoryDefinition:
    key: str
    label: str
    description: str
    default_threshold: float


@dataclass(frozen=True, slots=True)
class LabelDefinition:
    key: str
    label: str
    description: str
    default_skip: bool = True
    default_detect: bool = True


_VIDANGEL_CATEGORY_ROWS = get_vidangel_category_rows()
_VIDANGEL_LEAF_LOOKUP = get_vidangel_leaf_lookup()

DEFAULT_PROFANITY_TERMS = [
    "asshole",
    "bastard",
    "bitch",
    "bullshit",
    "cock",
    "cocksucker",
    "dammit",
    "damn",
    "dick",
    "fag",
    "faggot",
    "fuck",
    "fucker",
    "fucking",
    "goddamn",
    "hell",
    "mother fucker",
    "motherfucker",
    "piss",
    "pissed off",
    "prick",
    "slut",
    "son of a bitch",
    "shit",
    "whore",
]

DEFAULT_PROFANITY_ALLOWLIST = [
    "cockatiel",
    "cockatoo",
    "shitake",
]

CATEGORY_DEFINITIONS = tuple(
    CategoryDefinition(
        key=str(row["key"]),
        label=str(row["label"]),
        description=str(row["description"]),
        default_threshold=float(row["default_threshold"]),
    )
    for row in _VIDANGEL_CATEGORY_ROWS
)
SUPPORTED_CATEGORIES = tuple(definition.key for definition in CATEGORY_DEFINITIONS)
PREFERENCE_CATEGORIES = SUPPORTED_CATEGORIES

CATEGORY_LABEL_DEFINITIONS: dict[str, tuple[LabelDefinition, ...]] = {
    str(row["key"]): tuple(
        LabelDefinition(
            key=str(label["key"]),
            label=str(label["label"]),
            description=str(label["description"]),
            default_skip=bool(label.get("default_skip", True)),
            default_detect=bool(label.get("default_detect", False)),
        )
        for label in row.get("labels", [])
    )
    for row in _VIDANGEL_CATEGORY_ROWS
}

DEFAULT_CATEGORY_THRESHOLDS = {
    definition.key: definition.default_threshold for definition in CATEGORY_DEFINITIONS
}

DEFAULT_SKIP_LABELS = {
    category: [definition.key for definition in definitions if definition.default_skip]
    for category, definitions in CATEGORY_LABEL_DEFINITIONS.items()
}

# Scanner-backed VidAngel defaults. Categories without local scanner support keep
# detection disabled but remain fully playable via VidAngel sidecars.
_DETECT_DEFAULTS = {
    "sex_nudity_immodesty": {
        "nudity_female",
        "nudity_male",
        "nudity_both",
        "immodesty_female",
        "immodesty_male",
        "immodesty_both",
        "implied_nudity",
        "nudity_statues_and_paintings",
    },
    "sex_any": {
        "shown_w_nudity",
        "shown_w_o_nudity",
        "implied_not_shown",
        "sexually_suggestive",
    },
    "kissing": {
        "kissing_normal",
        "kissing_passion",
    },
    "violence_blood_gore": {
        "graphic",
        "gore",
        "non_graphic",
        "objectionable",
        "medical_graphic",
    },
    "alcohol_or_drug_use": {
        "drugs_illegal",
        "drugs_legal",
        "drugs_implied",
    },
}

DEFAULT_DETECT_LABELS = {
    category: [
        definition.key
        for definition in definitions
        if definition.key in _DETECT_DEFAULTS.get(category, set())
    ]
    for category, definitions in CATEGORY_LABEL_DEFINITIONS.items()
}

DETECTOR_STAGE_KEYS = (
    "nudity",
    "sexual_content",
    "profanity",
    "violence",
    "drugs",
)

SCAN_STAGE_DEFINITIONS = (
    {"key": "prepare", "order": 10, "label": "Prepare"},
    {"key": "nudity", "order": 20, "label": "Nudity Detector"},
    {"key": "sexual_content", "order": 30, "label": "Sex Detector"},
    {"key": "profanity", "order": 40, "label": "Language Detector"},
    {"key": "violence", "order": 50, "label": "Violence Detector"},
    {"key": "drugs", "order": 60, "label": "Drugs Detector"},
    {"key": "finalize", "order": 70, "label": "Finalize"},
)
SCAN_STAGE_ORDER = {
    str(stage["key"]): int(stage["order"]) for stage in SCAN_STAGE_DEFINITIONS
}


@dataclass(slots=True)
class MediaScanTarget:
    media_id: str
    plex_guid: str
    title: str
    file_path: str
    rating_key: str
    force_scan: bool = False


@dataclass(slots=True)
class SampledFrame:
    offset_ms: int
    jpeg_bytes: bytes


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
        category: str = "",
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
        self.category = category or (SUPPORTED_CATEGORIES[0] if SUPPORTED_CATEGORIES else "")
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

    def to_record(self, *, plex_guid: str | None = None) -> dict[str, Any]:
        return {
            "plex_guid": plex_guid or self.media_id,
            "media_id": self.media_id,
            "title": self.title,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "category": self.category,
            "source": self.source,
            "confidence": self.confidence,
            "labels": self.labels,
            "text_excerpt": self.text_excerpt,
            "thumbnail_path": self.thumbnail_path,
            "review_status": self.review_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Segment":
        return cls(
            media_id=str(row.get("media_id") or row.get("plex_guid") or ""),
            start_ms=int(row.get("start_ms") or 0),
            end_ms=int(row.get("end_ms") or 0),
            category=str(row.get("category") or ""),
            source=str(row.get("source") or "sidecar"),
            confidence=float(row["confidence"]) if row.get("confidence") is not None else None,
            text_excerpt=str(row.get("text_excerpt") or "") or None,
            title=str(row.get("title") or ""),
            thumbnail_path=str(row.get("thumbnail_path") or "") or None,
            labels=str(row.get("labels") or ""),
            review_status=str(row.get("review_status") or "pending"),
            created_at=str(row.get("created_at") or "") or None,
            updated_at=str(row.get("updated_at") or "") or None,
        )


MediaSegment = Segment


def get_vidangel_leaf_metadata(leaf_key: str) -> dict[str, Any] | None:
    """Return VidAngel taxonomy metadata for one exact leaf key."""
    return _VIDANGEL_LEAF_LOOKUP.get(str(leaf_key).strip())
