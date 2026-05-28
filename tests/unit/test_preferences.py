"""Unit tests for category preference resolution helpers."""

from __future__ import annotations

from leapfrog.domain import DEFAULT_CATEGORY_THRESHOLDS, PREFERENCE_CATEGORIES, SUPPORTED_CATEGORIES, Segment
from leapfrog.preferences import (
    build_user_label_preferences_map,
    build_user_category_preferences_map,
    build_user_filter_map,
    get_effective_skip_segments,
    get_threshold_for_category,
    is_category_enabled,
    resolve_preferences_for_users,
    resolve_user_category_preferences,
    resolve_user_label_preferences,
)


def test_canonical_category_sets_match():
    assert SUPPORTED_CATEGORIES == PREFERENCE_CATEGORIES
    assert "sex_nudity_immodesty" in SUPPORTED_CATEGORIES
    assert "language_profanity" in SUPPORTED_CATEGORIES
    assert "violence_blood_gore" in SUPPORTED_CATEGORIES


def test_resolve_user_category_preferences_defaults_to_all_disabled_without_saved_preferences():
    resolved = resolve_user_category_preferences(
        overall_enabled=True,
        stored_preferences={},
        threshold_defaults=DEFAULT_CATEGORY_THRESHOLDS,
    )
    assert resolved["sex_nudity_immodesty"]["enabled"] is False
    assert resolved["sex_nudity_immodesty"]["threshold"] == 0.4
    assert resolved["language_profanity"]["enabled"] is False
    assert resolved["language_profanity"]["threshold"] == 0.5


def test_resolve_user_category_preferences_disables_unspecified_categories_once_user_has_saved_preferences():
    resolved = resolve_user_category_preferences(
        overall_enabled=True,
        stored_preferences={"language_profanity": {"enabled": True, "threshold": 0.7}},
        threshold_defaults=DEFAULT_CATEGORY_THRESHOLDS,
    )
    assert resolved["sex_nudity_immodesty"]["enabled"] is False
    assert resolved["sex_any"]["enabled"] is False
    assert resolved["language_profanity"]["enabled"] is True
    assert resolved["language_profanity"]["threshold"] == 0.7


def test_get_effective_skip_segments_respects_category_and_threshold():
    selected = get_effective_skip_segments(
        [
            Segment(
                media_id="g1",
                start_time=1.0,
                end_time=2.0,
                category="sex_nudity_immodesty",
                source="nudenet",
                confidence=0.7,
            ),
            Segment(
                media_id="g1",
                start_time=3.0,
                end_time=4.0,
                category="language_profanity",
                source="subtitles",
                confidence=0.4,
            ),
            Segment(
                media_id="g1",
                start_time=5.0,
                end_time=6.0,
                category="language_profanity",
                source="subtitles",
                confidence=0.8,
            ),
        ],
        {
            "sex_nudity_immodesty": {"enabled": False, "threshold": 0.5},
            "language_profanity": {"enabled": True, "threshold": 0.5},
        },
    )
    assert len(selected) == 1
    assert selected[0]["category"] == "language_profanity"
    assert selected[0]["confidence"] == 0.8


def test_category_helpers_report_enabled_and_threshold():
    preferences = {
        "sex_nudity_immodesty": {"enabled": True, "threshold": 0.6},
        "language_profanity": {"enabled": False, "threshold": 0.5},
    }
    assert is_category_enabled(preferences, "sex_nudity_immodesty") is True
    assert is_category_enabled(preferences, "language_profanity") is False
    assert get_threshold_for_category(preferences, "sex_nudity_immodesty") == 0.6
    assert get_threshold_for_category(preferences, "violence_blood_gore") is None


def test_build_user_preference_maps_group_rows_by_user_and_category():
    filters = build_user_filter_map(
        [
            {"plex_username": "alice", "enabled": 1},
            {"plex_username": "bob", "enabled": 0},
        ]
    )
    stored = build_user_category_preferences_map(
        [
            {"user_id": "alice", "category": "sex_nudity_immodesty", "enabled": 1, "threshold": 0.6},
            {"user_id": "alice", "category": "language_profanity", "enabled": 0, "threshold": 0.5},
            {"user_id": "bob", "category": "language_profanity", "enabled": 1, "threshold": 0.7},
        ]
    )

    assert filters == {"alice": True, "bob": False}
    assert stored["alice"]["sex_nudity_immodesty"]["threshold"] == 0.6
    assert stored["bob"]["language_profanity"]["enabled"] == 1


def test_resolve_preferences_for_users_reuses_shared_filter_inputs():
    resolved = resolve_preferences_for_users(
        ["alice", "bob"],
        overall_filters={"alice": True, "bob": False},
        stored_preferences_by_user={
            "alice": {"language_profanity": {"enabled": True, "threshold": 0.8}},
        },
        threshold_defaults=DEFAULT_CATEGORY_THRESHOLDS,
    )

    assert resolved["alice"]["language_profanity"]["enabled"] is True
    assert resolved["alice"]["sex_nudity_immodesty"]["enabled"] is False
    assert resolved["bob"]["sex_nudity_immodesty"]["enabled"] is False


def test_resolve_user_label_preferences_uses_granular_defaults_and_stored_overrides():
    stored = build_user_label_preferences_map(
        [
            {
                "user_id": "alice",
                "category": "sex_any",
                "label": "shown_w_nudity",
                "enabled": 0,
                "threshold": None,
            },
            {
                "user_id": "alice",
                "category": "kissing",
                "label": "kissing_normal",
                "enabled": 1,
                "threshold": None,
            },
        ]
    )

    resolved = resolve_user_label_preferences(
        stored_label_preferences=stored["alice"],
        default_skip_labels={"sex_any": ["shown_w_nudity"], "kissing": ["kissing_normal"]},
    )

    assert resolved["sex_any"]["shown_w_nudity"]["enabled"] is False
    assert resolved["kissing"]["kissing_normal"]["enabled"] is True
    assert resolved["kissing"]["kissing_passion"]["enabled"] is False


def test_effective_skip_segments_respects_granular_labels_when_present():
    selected = get_effective_skip_segments(
        [
            Segment(
                media_id="g1",
                start_time=1.0,
                end_time=2.0,
                category="kissing",
                source="semantic_clip",
                confidence=0.9,
                labels="kissing_normal",
            ),
            Segment(
                media_id="g1",
                start_time=3.0,
                end_time=4.0,
                category="sex_any",
                source="semantic_clip",
                confidence=0.9,
                labels="shown_w_nudity",
            ),
        ],
        {
            "kissing": {
                "enabled": True,
                "threshold": 0.5,
                "labels": {
                    "kissing_normal": {"enabled": False, "threshold": None},
                    "kissing_passion": {"enabled": True, "threshold": None},
                },
            },
            "sex_any": {
                "enabled": True,
                "threshold": 0.5,
                "labels": {
                    "shown_w_nudity": {"enabled": True, "threshold": None},
                },
            },
        },
    )

    assert len(selected) == 1
    assert selected[0]["labels"] == "shown_w_nudity"


def test_effective_skip_segments_keeps_unknown_legacy_labels_category_gated():
    selected = get_effective_skip_segments(
        [
            Segment(
                media_id="g1",
                start_time=1.0,
                end_time=2.0,
                category="violence_blood_gore",
                source="semantic_clip",
                confidence=0.9,
                labels="weapon",
            ),
        ],
        {
            "violence_blood_gore": {
                "enabled": True,
                "threshold": 0.5,
                "labels": {
                    "weapon_threat": {"enabled": False, "threshold": None},
                },
            },
        },
    )

    assert len(selected) == 1
