"""Resolve effective per-user category preferences for playback and UI."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import database as db
from .domain import (
    DEFAULT_CATEGORY_THRESHOLDS,
    PREFERENCE_CATEGORIES,
    Segment,
)


def resolve_user_category_preferences(
    *,
    overall_enabled: bool,
    stored_preferences: dict[str, dict],
    nudity_threshold: float,
    profanity_threshold: float,
) -> dict[str, dict[str, float | bool]]:
    """Return effective category preferences for a single user."""
    defaults = {
        "nudity": nudity_threshold,
        "profanity": profanity_threshold,
    }
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


def resolve_preferences_for_users(
    user_ids: Iterable[str],
    *,
    overall_filters: dict[str, bool],
    stored_preferences_by_user: dict[str, dict[str, dict[str, Any]]],
    nudity_threshold: float,
    profanity_threshold: float,
) -> dict[str, dict[str, dict[str, float | bool]]]:
    """Resolve effective category preferences for multiple users at once."""
    resolved: dict[str, dict[str, dict[str, float | bool]]] = {}
    for user_id in {str(value).strip() for value in user_ids if str(value).strip()}:
        resolved[user_id] = resolve_user_category_preferences(
            overall_enabled=overall_filters.get(user_id, True),
            stored_preferences=stored_preferences_by_user.get(user_id, {}),
            nudity_threshold=nudity_threshold,
            profanity_threshold=profanity_threshold,
        )
    return resolved


async def get_preference_threshold_settings() -> tuple[float, float]:
    """Return the default nudity and profanity thresholds from settings."""
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
    return nudity_threshold, profanity_threshold


async def get_resolved_preferences_for_users(
    user_ids: Iterable[str],
    *,
    nudity_threshold: float,
    profanity_threshold: float,
) -> dict[str, dict[str, dict[str, float | bool]]]:
    """Load and resolve effective playback preferences for multiple users."""
    overall_filters = build_user_filter_map(await db.get_all_user_filters())
    stored_preferences = build_user_category_preferences_map(
        await db.get_all_user_preferences()
    )
    return resolve_preferences_for_users(
        user_ids,
        overall_filters=overall_filters,
        stored_preferences_by_user=stored_preferences,
        nudity_threshold=nudity_threshold,
        profanity_threshold=profanity_threshold,
    )


async def get_resolved_preferences_for_user(
    user_id: str,
    *,
    nudity_threshold: float,
    profanity_threshold: float,
) -> dict[str, dict[str, float | bool | None]]:
    """Load and resolve effective playback preferences for one user."""
    overall_filter = await db.get_user_filter(user_id)
    stored_preferences = build_user_category_preferences_map(
        await db.get_user_preferences(user_id)
    ).get(user_id, {})
    return resolve_user_category_preferences(
        overall_enabled=overall_filter is None or bool(overall_filter["enabled"]),
        stored_preferences=stored_preferences,
        nudity_threshold=nudity_threshold,
        profanity_threshold=profanity_threshold,
    )


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
