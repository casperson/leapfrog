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
    assert SUPPORTED_CATEGORIES == (
        "nudity",
        "sexual_content",
        "profanity",
        "violence",
        "drugs",
    )


def test_resolve_user_category_preferences_defaults_to_nudity_only_without_saved_preferences():
    resolved = resolve_user_category_preferences(
        overall_enabled=True,
        stored_preferences={},
        threshold_defaults=DEFAULT_CATEGORY_THRESHOLDS,
    )
    assert resolved["nudity"]["enabled"] is True
    assert resolved["nudity"]["threshold"] == 0.4
    assert resolved["sexual_content"]["enabled"] is False
    assert resolved["profanity"]["enabled"] is False
    assert resolved["profanity"]["threshold"] == 0.5


def test_resolve_user_category_preferences_disables_unspecified_categories_once_user_has_saved_preferences():
    resolved = resolve_user_category_preferences(
        overall_enabled=True,
        stored_preferences={"profanity": {"enabled": True, "threshold": 0.7}},
        threshold_defaults=DEFAULT_CATEGORY_THRESHOLDS,
    )
    assert resolved["nudity"]["enabled"] is False
    assert resolved["sexual_content"]["enabled"] is False
    assert resolved["profanity"]["enabled"] is True
    assert resolved["profanity"]["threshold"] == 0.7


def test_get_effective_skip_segments_respects_category_and_threshold():
    selected = get_effective_skip_segments(
        [
            Segment(
                media_id="g1",
                start_time=1.0,
                end_time=2.0,
                category="nudity",
                source="nudenet",
                confidence=0.7,
            ),
            Segment(
                media_id="g1",
                start_time=3.0,
                end_time=4.0,
                category="profanity",
                source="subtitles",
                confidence=0.4,
            ),
            Segment(
                media_id="g1",
                start_time=5.0,
                end_time=6.0,
                category="profanity",
                source="subtitles",
                confidence=0.8,
            ),
        ],
        {
            "nudity": {"enabled": False, "threshold": 0.5},
            "profanity": {"enabled": True, "threshold": 0.5},
        },
    )
    assert len(selected) == 1
    assert selected[0]["category"] == "profanity"
    assert selected[0]["confidence"] == 0.8


def test_category_helpers_report_enabled_and_threshold():
    preferences = {
        "nudity": {"enabled": True, "threshold": 0.6},
        "profanity": {"enabled": False, "threshold": 0.5},
    }
    assert is_category_enabled(preferences, "nudity") is True
    assert is_category_enabled(preferences, "profanity") is False
    assert get_threshold_for_category(preferences, "nudity") == 0.6
    assert get_threshold_for_category(preferences, "violence") is None


def test_build_user_preference_maps_group_rows_by_user_and_category():
    filters = build_user_filter_map(
        [
            {"plex_username": "alice", "enabled": 1},
            {"plex_username": "bob", "enabled": 0},
        ]
    )
    stored = build_user_category_preferences_map(
        [
            {"user_id": "alice", "category": "nudity", "enabled": 1, "threshold": 0.6},
            {"user_id": "alice", "category": "profanity", "enabled": 0, "threshold": 0.5},
            {"user_id": "bob", "category": "profanity", "enabled": 1, "threshold": 0.7},
        ]
    )

    assert filters == {"alice": True, "bob": False}
    assert stored["alice"]["nudity"]["threshold"] == 0.6
    assert stored["bob"]["profanity"]["enabled"] == 1


def test_resolve_preferences_for_users_reuses_shared_filter_inputs():
    resolved = resolve_preferences_for_users(
        ["alice", "bob"],
        overall_filters={"alice": True, "bob": False},
        stored_preferences_by_user={
            "alice": {"profanity": {"enabled": True, "threshold": 0.8}},
        },
        threshold_defaults=DEFAULT_CATEGORY_THRESHOLDS,
    )

    assert resolved["alice"]["profanity"]["enabled"] is True
    assert resolved["alice"]["nudity"]["enabled"] is False
    assert resolved["bob"]["nudity"]["enabled"] is False


def test_resolve_user_label_preferences_uses_granular_defaults_and_stored_overrides():
    stored = build_user_label_preferences_map(
        [
            {
                "user_id": "alice",
                "category": "sexual_content",
                "label": "explicit_sex",
                "enabled": 0,
                "threshold": None,
            },
            {
                "user_id": "alice",
                "category": "sexual_content",
                "label": "brief_kiss",
                "enabled": 1,
                "threshold": None,
            },
        ]
    )

    resolved = resolve_user_label_preferences(
        stored_label_preferences=stored["alice"],
        default_skip_labels={"sexual_content": ["explicit_sex"]},
    )

    assert resolved["sexual_content"]["explicit_sex"]["enabled"] is False
    assert resolved["sexual_content"]["brief_kiss"]["enabled"] is True
    assert resolved["sexual_content"]["romantic_kiss"]["enabled"] is False


def test_effective_skip_segments_respects_granular_labels_when_present():
    selected = get_effective_skip_segments(
        [
            Segment(
                media_id="g1",
                start_time=1.0,
                end_time=2.0,
                category="sexual_content",
                source="semantic_clip",
                confidence=0.9,
                labels="brief_kiss",
            ),
            Segment(
                media_id="g1",
                start_time=3.0,
                end_time=4.0,
                category="sexual_content",
                source="semantic_clip",
                confidence=0.9,
                labels="explicit_sex",
            ),
        ],
        {
            "sexual_content": {
                "enabled": True,
                "threshold": 0.5,
                "labels": {
                    "brief_kiss": {"enabled": False, "threshold": None},
                    "explicit_sex": {"enabled": True, "threshold": None},
                },
            },
        },
    )

    assert len(selected) == 1
    assert selected[0]["labels"] == "explicit_sex"


def test_effective_skip_segments_keeps_unknown_legacy_labels_category_gated():
    selected = get_effective_skip_segments(
        [
            Segment(
                media_id="g1",
                start_time=1.0,
                end_time=2.0,
                category="violence",
                source="semantic_clip",
                confidence=0.9,
                labels="weapon",
            ),
        ],
        {
            "violence": {
                "enabled": True,
                "threshold": 0.5,
                "labels": {
                    "weapon_threat": {"enabled": False, "threshold": None},
                },
            },
        },
    )

    assert len(selected) == 1
