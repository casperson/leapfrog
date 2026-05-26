"""Resolve effective per-user category preferences for playback and UI."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from . import database as db
from .domain import (
    CATEGORY_LABEL_DEFINITIONS,
    CATEGORY_DEFINITIONS,
    DEFAULT_CATEGORY_THRESHOLDS,
    DEFAULT_SKIP_LABELS,
    PREFERENCE_CATEGORIES,
    Segment,
)
from .vidangel_export import load_vidangel_tag_definition_records


def resolve_user_category_preferences(
    *,
    overall_enabled: bool,
    stored_preferences: dict[str, dict],
    threshold_defaults: dict[str, float] | None = None,
) -> dict[str, dict[str, float | bool]]:
    """Return effective category preferences for a single user."""
    defaults = dict(DEFAULT_CATEGORY_THRESHOLDS)
    if threshold_defaults:
        defaults.update(
            {
                str(category): float(value)
                for category, value in threshold_defaults.items()
                if category in PREFERENCE_CATEGORIES
            }
        )
    categories = set(PREFERENCE_CATEGORIES) | set(stored_preferences)
    has_stored_preferences = bool(stored_preferences)
    resolved: dict[str, dict[str, float | bool]] = {}
    for category in sorted(categories):
        raw = stored_preferences.get(category, {})
        threshold = raw.get("threshold")
        default_enabled = category == "nudity" and not has_stored_preferences
        resolved[category] = {
            "enabled": bool(overall_enabled and raw.get("enabled", default_enabled)),
            "threshold": (
                float(threshold)
                if threshold is not None
                else defaults.get(category, DEFAULT_CATEGORY_THRESHOLDS.get(category))
            ),
        }
    return resolved


def build_user_filter_map(rows: Iterable[dict[str, Any]]) -> dict[str, bool]:
    """Normalize stored user filter rows into a bool map keyed by username."""
    return {
        str(row.get("plex_username") or ""): bool(row.get("enabled"))
        for row in rows
        if row.get("plex_username")
    }


def build_user_category_preferences_map(
    rows: Iterable[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Group stored category preference rows by user id and category."""
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        user_id = str(row.get("user_id") or "").strip()
        category = str(row.get("category") or "").strip()
        if not user_id or not category:
            continue
        grouped.setdefault(user_id, {})[category] = dict(row)
    return grouped


def build_user_label_preferences_map(
    rows: Iterable[dict[str, Any]],
) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    """Group stored label preference rows by user, category, and label."""
    grouped: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for row in rows:
        user_id = str(row.get("user_id") or "").strip()
        category = str(row.get("category") or "").strip()
        label = str(row.get("label") or "").strip()
        if not user_id or not category or not label:
            continue
        grouped.setdefault(user_id, {}).setdefault(category, {})[label] = dict(row)
    return grouped


def resolve_user_label_preferences(
    *,
    stored_label_preferences: dict[str, dict[str, Any]],
    label_definitions_by_category: dict[str, list[dict[str, Any]]] | None = None,
    default_skip_labels: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, dict[str, float | bool | None]]]:
    """Return effective label preferences grouped by category and label."""
    defaults = default_skip_labels or DEFAULT_SKIP_LABELS
    resolved: dict[str, dict[str, dict[str, float | bool | None]]] = {}
    metadata = label_definitions_by_category or {
        category: [
            {
                "key": definition.key,
                "label": definition.label,
                "description": definition.description,
                "default_skip": definition.default_skip,
                "default_detect": definition.default_detect,
            }
            for definition in definitions
        ]
        for category, definitions in CATEGORY_LABEL_DEFINITIONS.items()
    }
    for category, definitions in metadata.items():
        category_defaults = set(defaults.get(category, []))
        stored_for_category = stored_label_preferences.get(category, {})
        resolved[category] = {}
        for definition in definitions:
            label_key = str(definition.get("key") or "").strip()
            if not label_key:
                continue
            raw = stored_for_category.get(label_key, {})
            threshold = raw.get("threshold")
            resolved[category][label_key] = {
                "enabled": bool(raw.get("enabled", label_key in category_defaults)),
                "threshold": float(threshold) if threshold is not None else None,
            }
    return resolved


def resolve_preferences_for_users(
    user_ids: Iterable[str],
    *,
    overall_filters: dict[str, bool],
    stored_preferences_by_user: dict[str, dict[str, dict[str, Any]]],
    threshold_defaults: dict[str, float] | None = None,
    stored_label_preferences_by_user: dict[str, dict[str, dict[str, dict[str, Any]]]] | None = None,
    label_definitions_by_category: dict[str, list[dict[str, Any]]] | None = None,
    default_skip_labels: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Resolve effective category preferences for multiple users at once."""
    resolved: dict[str, dict[str, dict[str, Any]]] = {}
    label_preferences_by_user = stored_label_preferences_by_user or {}
    for user_id in {str(value).strip() for value in user_ids if str(value).strip()}:
        user_preferences = resolve_user_category_preferences(
            overall_enabled=overall_filters.get(user_id, True),
            stored_preferences=stored_preferences_by_user.get(user_id, {}),
            threshold_defaults=threshold_defaults,
        )
        label_preferences = resolve_user_label_preferences(
            stored_label_preferences=label_preferences_by_user.get(user_id, {}),
            label_definitions_by_category=label_definitions_by_category,
            default_skip_labels=default_skip_labels,
        )
        for category, preferences in user_preferences.items():
            preferences["labels"] = label_preferences.get(category, {})
        resolved[user_id] = user_preferences
    return resolved


async def get_preference_threshold_settings() -> dict[str, float]:
    """Return per-category default thresholds used for user preference resolution."""
    nudity_threshold = float(
        await db.get_setting(
            "confidence_threshold",
            str(DEFAULT_CATEGORY_THRESHOLDS["nudity"]),
        )
    )
    profanity_threshold = float(
        await db.get_setting(
            "default_profanity_threshold",
            str(DEFAULT_CATEGORY_THRESHOLDS["profanity"]),
        )
    )
    defaults = dict(DEFAULT_CATEGORY_THRESHOLDS)
    defaults["nudity"] = nudity_threshold
    defaults["profanity"] = profanity_threshold
    return defaults


async def get_default_skip_label_settings() -> dict[str, list[str]]:
    """Return server default labels that should skip when a category is enabled."""
    raw = await db.get_setting("default_skip_labels", json.dumps(DEFAULT_SKIP_LABELS))
    category_metadata = await get_category_metadata()
    valid_labels_by_category = {
        str(category["key"]): {str(label["key"]) for label in category.get("labels", [])}
        for category in category_metadata
    }
    try:
        parsed = json.loads(raw)
    except Exception:
        return DEFAULT_SKIP_LABELS
    if not isinstance(parsed, dict):
        return DEFAULT_SKIP_LABELS
    result = dict(DEFAULT_SKIP_LABELS)
    for category, labels in parsed.items():
        if category not in valid_labels_by_category or not isinstance(labels, list):
            continue
        valid_labels = valid_labels_by_category[category]
        result[str(category)] = [str(label) for label in labels if str(label) in valid_labels]
    return result


async def get_category_metadata() -> list[dict[str, Any]]:
    """Return canonical category metadata merged with dynamic VidAngel label definitions."""
    default_skip_labels = DEFAULT_SKIP_LABELS
    category_rows = [
        {
            "key": definition.key,
            "label": definition.label,
            "description": definition.description,
            "default_threshold": definition.default_threshold,
            "labels": [
                {
                    "key": label.key,
                    "label": label.label,
                    "description": label.description,
                    "default_skip": label.key in default_skip_labels.get(definition.key, []),
                    "default_detect": label.default_detect,
                }
                for label in CATEGORY_LABEL_DEFINITIONS.get(definition.key, ())
            ],
        }
        for definition in CATEGORY_DEFINITIONS
    ]
    categories_by_key = {str(row["key"]): row for row in category_rows}
    for record in load_vidangel_tag_definition_records():
        category_key = str(record.get("mapped_category") or "").strip()
        if category_key not in categories_by_key:
            continue
        vidangel_key = f"vidangel:{str(record.get('key') or '').strip()}"
        if vidangel_key == "vidangel:":
            continue
        labels = categories_by_key[category_key].setdefault("labels", [])
        if any(str(label.get("key")) == vidangel_key for label in labels):
            continue
        path_titles = record.get("path_titles") or []
        if isinstance(path_titles, list):
            path_text = " > ".join(str(part) for part in path_titles if str(part).strip())
        else:
            path_text = str(path_titles or "")
        example = str(record.get("example_description") or "").strip()
        description = path_text
        if example:
            description = f"{path_text}. Example: {example}" if path_text else example
        labels.append(
            {
                "key": vidangel_key,
                "label": str(record.get("display_title") or record.get("key") or vidangel_key),
                "description": description or "VidAngel filter event.",
                "default_skip": False,
                "default_detect": False,
            }
        )
    for row in category_rows:
        row["labels"] = sorted(
            row.get("labels", []),
            key=lambda label: (0 if not str(label.get("key", "")).startswith("vidangel:") else 1, str(label.get("label") or "").lower()),
        )
    return category_rows


async def get_resolved_preferences_for_users(
    user_ids: Iterable[str],
    *,
    threshold_defaults: dict[str, float] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Load and resolve effective playback preferences for multiple users."""
    overall_filters = build_user_filter_map(await db.get_all_user_filters())
    stored_preferences = build_user_category_preferences_map(
        await db.get_all_user_preferences()
    )
    stored_label_preferences = build_user_label_preferences_map(
        await db.get_all_user_label_preferences()
    )
    category_metadata = await get_category_metadata()
    label_definitions_by_category = {
        str(category["key"]): [dict(label) for label in category.get("labels", [])]
        for category in category_metadata
    }
    return resolve_preferences_for_users(
        user_ids,
        overall_filters=overall_filters,
        stored_preferences_by_user=stored_preferences,
        threshold_defaults=threshold_defaults,
        stored_label_preferences_by_user=stored_label_preferences,
        label_definitions_by_category=label_definitions_by_category,
        default_skip_labels=await get_default_skip_label_settings(),
    )


async def get_resolved_preferences_for_user(
    user_id: str,
    *,
    threshold_defaults: dict[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load and resolve effective playback preferences for one user."""
    overall_filter = await db.get_user_filter(user_id)
    stored_preferences = build_user_category_preferences_map(
        await db.get_user_preferences(user_id)
    ).get(user_id, {})
    stored_label_preferences = build_user_label_preferences_map(
        await db.get_user_label_preferences(user_id)
    ).get(user_id, {})
    category_metadata = await get_category_metadata()
    label_definitions_by_category = {
        str(category["key"]): [dict(label) for label in category.get("labels", [])]
        for category in category_metadata
    }
    category_preferences = resolve_user_category_preferences(
        overall_enabled=overall_filter is None or bool(overall_filter["enabled"]),
        stored_preferences=stored_preferences,
        threshold_defaults=threshold_defaults,
    )
    label_preferences = resolve_user_label_preferences(
        stored_label_preferences=stored_label_preferences,
        label_definitions_by_category=label_definitions_by_category,
        default_skip_labels=await get_default_skip_label_settings(),
    )
    for category, preferences in category_preferences.items():
        preferences["labels"] = label_preferences.get(category, {})
    return category_preferences


def is_category_enabled(
    preferences: dict[str, dict[str, float | bool]],
    category: str,
) -> bool:
    """Return whether a category is enabled in resolved preferences."""
    pref = preferences.get(category)
    return bool(pref and pref.get("enabled", False))


def get_threshold_for_category(
    preferences: dict[str, dict[str, float | bool | None]],
    category: str,
) -> float | None:
    """Return the resolved threshold for a category, if any."""
    pref = preferences.get(category)
    if pref is None:
        return None
    threshold = pref.get("threshold")
    return float(threshold) if threshold is not None else None


def _split_segment_labels(labels: Any) -> list[str]:
    return [
        label.strip()
        for label in str(labels or "").split(",")
        if label.strip()
    ]


def is_segment_label_enabled(
    preferences: dict[str, dict[str, Any]],
    category: str,
    labels: list[str],
) -> bool:
    """Return whether at least one segment label is enabled for playback."""
    category_preferences = preferences.get(category, {})
    label_preferences = category_preferences.get("labels")
    if not isinstance(label_preferences, dict) or not label_preferences:
        return True
    if not labels:
        return True
    for label in labels:
        raw = label_preferences.get(label)
        if raw is None:
            continue
        if bool(raw.get("enabled", False)):
            return True
    # Preserve older scans whose labels predate the granular taxonomy. Category
    # preferences still gate these segments until they are rescanned.
    known_labels = {str(label) for label in label_preferences}
    return not any(label in known_labels for label in labels)


def _segment_record(segment: Segment | dict[str, Any]) -> dict[str, Any]:
    if isinstance(segment, Segment):
        return segment.to_record(plex_guid=segment.media_id)
    record = dict(segment)
    normalized = Segment.from_row(record).to_record(plex_guid=record.get("plex_guid"))
    record.update(normalized)
    return record


def get_effective_skip_segments(
    segments: list[Segment | dict[str, Any]],
    preferences: dict[str, dict[str, float | bool | None]],
) -> list[dict[str, Any]]:
    """Return the effective skip list for a user and media item."""
    selected: list[dict] = []
    for segment in segments:
        record = _segment_record(segment)
        category = str(record.get("category") or "nudity")
        if not is_category_enabled(preferences, category):
            continue
        labels = _split_segment_labels(record.get("labels"))
        if not is_segment_label_enabled(preferences, category, labels):
            continue
        threshold = get_threshold_for_category(preferences, category)
        confidence = record.get("confidence")
        if threshold is not None and confidence is not None and float(confidence) < threshold:
            continue
        selected.append(record)
    return selected


def filter_segments_for_preferences(
    segments: list[Segment | dict[str, Any]],
    preferences: dict[str, dict[str, float | bool | None]],
) -> list[dict[str, Any]]:
    """Backward-compatible alias for effective skip segment filtering."""
    return get_effective_skip_segments(segments, preferences)
