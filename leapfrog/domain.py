"""Shared domain models for segments, scan targets, and status records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


CATEGORY_DEFINITIONS = (
    CategoryDefinition(
        key="nudity",
        label="Nudity",
        description="Exposed body-part nudity detected from sampled frames.",
        default_threshold=0.4,
    ),
    CategoryDefinition(
        key="sexual_content",
        label="Sexual Content",
        description="Sexual activity or intimate content that may not include nudity.",
        default_threshold=0.5,
    ),
    CategoryDefinition(
        key="profanity",
        label="Profanity",
        description="Profanity matched from subtitles or local transcript fallback.",
        default_threshold=0.5,
    ),
    CategoryDefinition(
        key="violence",
        label="Violence",
        description="Violence, weapons, blood, or similar harmful acts detected from frames.",
        default_threshold=0.5,
    ),
    CategoryDefinition(
        key="drugs",
        label="Drugs",
        description="Drug use or paraphernalia detected from sampled frames.",
        default_threshold=0.5,
    ),
)
SUPPORTED_CATEGORIES = tuple(definition.key for definition in CATEGORY_DEFINITIONS)
PREFERENCE_CATEGORIES = SUPPORTED_CATEGORIES

CATEGORY_LABEL_DEFINITIONS: dict[str, tuple[LabelDefinition, ...]] = {
    "nudity": (
        LabelDefinition("FEMALE_GENITALIA_EXPOSED", "Female Genitalia", "Exposed female genitalia."),
        LabelDefinition("MALE_GENITALIA_EXPOSED", "Male Genitalia", "Exposed male genitalia."),
        LabelDefinition("FEMALE_BREAST_EXPOSED", "Female Breast", "Exposed female breast."),
        LabelDefinition("ANUS_EXPOSED", "Anus", "Exposed anus."),
        LabelDefinition("BUTTOCKS_EXPOSED", "Buttocks", "Exposed buttocks."),
        LabelDefinition("MALE_BREAST_EXPOSED", "Male Breast", "Exposed male chest.", default_skip=False),
        LabelDefinition("FEMALE_GENITALIA_COVERED", "Covered Female Genitalia", "Covered female genitalia.", default_skip=False),
        LabelDefinition("FEMALE_BREAST_COVERED", "Covered Female Breast", "Covered female breast.", default_skip=False),
        LabelDefinition("MALE_BREAST_COVERED", "Covered Male Breast", "Covered male chest.", default_skip=False),
        LabelDefinition("BUTTOCKS_COVERED", "Covered Buttocks", "Covered buttocks.", default_skip=False),
    ),
    "sexual_content": (
        LabelDefinition("explicit_sex", "Explicit Sex", "Visible explicit sexual activity."),
        LabelDefinition("simulated_sex", "Simulated Sex", "Simulated sex or thrusting without explicit nudity."),
        LabelDefinition("oral_sex", "Oral Sex", "Oral sex activity or framing."),
        LabelDefinition("masturbation", "Masturbation", "Masturbation or self-stimulation."),
        LabelDefinition("sexual_touching", "Sexual Touching", "Sexualized touching of intimate body areas."),
        LabelDefinition("intimate_touch", "Intimate Touch", "Suggestive intimate physical contact.", default_skip=False, default_detect=False),
        LabelDefinition("bed_intimacy", "Bed Intimacy", "Implied sexual activity or intimacy in bed.", default_skip=False, default_detect=False),
        LabelDefinition("heavy_making_out", "Heavy Making Out", "Extended passionate kissing or making out.", default_skip=False, default_detect=False),
        LabelDefinition("romantic_kiss", "Romantic Kiss", "A romantic kiss."),
        LabelDefinition("brief_kiss", "Brief Kiss", "Brief peck or non-explicit kiss.", default_skip=False, default_detect=False),
        LabelDefinition("lingerie", "Lingerie", "Sexualized lingerie or underwear scene.", default_skip=False, default_detect=False),
        LabelDefinition("striptease", "Striptease", "Striptease or erotic undressing."),
    ),
    "violence": (
        LabelDefinition("graphic_violence", "Graphic Violence", "Graphic violence or gore."),
        LabelDefinition("blood", "Blood", "Visible blood or bloody injury."),
        LabelDefinition("fight", "Fight", "Physical fighting, punching, kicking, or brawling."),
        LabelDefinition("weapon_threat", "Weapon Threat", "Weapon pointed or used as a threat."),
        LabelDefinition("gunfire", "Gunfire", "Gunfire or shooting."),
        LabelDefinition("stabbing", "Stabbing", "Stabbing or knife attack."),
        LabelDefinition("explosion", "Explosion", "Explosion or blast."),
        LabelDefinition("dead_body", "Dead Body", "Corpse or dead body.", default_skip=False),
        LabelDefinition("disturbing_image", "Disturbing Image", "Disturbing non-graphic image.", default_skip=False),
        LabelDefinition("medical_injury", "Medical Injury", "Medical injury or wound treatment.", default_skip=False),
    ),
    "drugs": (
        LabelDefinition("hard_drug_use", "Hard Drug Use", "Visible illicit hard drug use."),
        LabelDefinition("needle", "Needle", "Needle injection or syringe use."),
        LabelDefinition("powder_drugs", "Powder Drugs", "Powdered drugs or lines."),
        LabelDefinition("pill_abuse", "Pill Abuse", "Pill misuse or abuse."),
        LabelDefinition("drug_paraphernalia", "Drug Paraphernalia", "Pipes, baggies, syringes, or drug equipment."),
        LabelDefinition("smoking_drugs", "Smoking Drugs", "Smoking illicit or suspicious substances."),
        LabelDefinition("marijuana", "Marijuana", "Marijuana use or cannabis products."),
        LabelDefinition("alcohol_abuse", "Alcohol Abuse", "Heavy alcohol abuse or intoxication.", default_skip=False),
        LabelDefinition("tobacco", "Tobacco", "Cigarette or tobacco smoking.", default_skip=False),
    ),
    "profanity": (),
}
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
CATEGORY_LABEL_DEFINITIONS["profanity"] = tuple(
    LabelDefinition(term, term, f"Profanity root or phrase: {term}.")
    for term in DEFAULT_PROFANITY_TERMS
)
DEFAULT_CATEGORY_THRESHOLDS = {
    definition.key: definition.default_threshold for definition in CATEGORY_DEFINITIONS
}

DEFAULT_SKIP_LABELS = {
    category: [
        definition.key
        for definition in definitions
        if definition.default_skip
    ]
    for category, definitions in CATEGORY_LABEL_DEFINITIONS.items()
}

DEFAULT_DETECT_LABELS = {
    category: [
        definition.key
        for definition in definitions
        if definition.default_detect
    ]
    for category, definitions in CATEGORY_LABEL_DEFINITIONS.items()
}

SCAN_STAGE_DEFINITIONS = (
    {"key": "prepare", "order": 10, "label": "Prepare"},
    {"key": "nudity", "order": 20, "label": "Nudity"},
    {"key": "sexual_content", "order": 30, "label": "Sexual Content"},
    {"key": "profanity", "order": 40, "label": "Profanity"},
    {"key": "violence", "order": 50, "label": "Violence"},
    {"key": "drugs", "order": 60, "label": "Drugs"},
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
    segment_count: int = 0
    progress: float = 0.0
