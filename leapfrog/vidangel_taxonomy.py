"""Load and normalize the VidAngel taxonomy used for categories and labels."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


_GROUP_LABEL_OVERRIDES = {
    "alcohol_or_drug_use": "Drugs & Alcohol",
    "credits": "Credits & Extras",
    "human_functions": "Medical & Body Process",
    "kissing": "Kissing",
    "language_blasphemy": "Blasphemy",
    "language_language_childish": "Childish Language",
    "language_language_racial": "Racial & Bigoted Slurs",
    "language_language_sexual": "Sexual Reference",
    "language_profanity": "Profanity",
    "language_profanity_captions": "Captions with Profanity",
    "sex_any": "Sex",
    "sex_nudity_immodesty": "Nudity & Immodesty",
    "violence_blood_gore": "Violence",
}

_GROUP_DESCRIPTION_OVERRIDES = {
    "alcohol_or_drug_use": "VidAngel drugs and alcohol filters.",
    "credits": "VidAngel credits and extras filters.",
    "human_functions": "VidAngel medical and body-process filters.",
    "kissing": "VidAngel kissing filters.",
    "language_blasphemy": "VidAngel blasphemy language filters.",
    "language_language_childish": "VidAngel childish language filters.",
    "language_language_racial": "VidAngel racial and bigoted slur filters.",
    "language_language_sexual": "VidAngel sexual-reference language filters.",
    "language_profanity": "VidAngel profanity language filters.",
    "language_profanity_captions": "VidAngel profanity-caption filters.",
    "sex_any": "VidAngel sex-content filters.",
    "sex_nudity_immodesty": "VidAngel nudity and immodesty filters.",
    "violence_blood_gore": "VidAngel violence and gore filters.",
}

# Keep the export UI in a predictable family-first order even when a title
# contains only a subset of VidAngel's categories.
_UI_CATEGORY_ORDER = (
    "language_profanity",
    "language_profanity_captions",
    "language_blasphemy",
    "language_language_racial",
    "language_language_childish",
    "sex_any",
    "sex_nudity_immodesty",
    "kissing",
    "language_language_sexual",
    "violence_blood_gore",
    "alcohol_or_drug_use",
    "human_functions",
    "credits",
)
_UI_CATEGORY_ORDER_INDEX = {key: index for index, key in enumerate(_UI_CATEGORY_ORDER)}

_UNCENSORED_CONNECTORS = {"a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "with"}


def _default_tag_definitions_path() -> Path:
    return Path(__file__).resolve().parent.parent / "vidangel" / "tag_definitions.json"


def normalize_vidangel_group_key(path_keys: list[str] | tuple[str, ...]) -> str:
    """Return the canonical VidAngel category-group key for a taxonomy path."""
    parts = [str(part).strip() for part in path_keys if str(part).strip()]
    if not parts:
        return ""
    if parts[0] == "language" and len(parts) > 1:
        return f"language_{parts[1]}"
    return parts[0]


def vidangel_ui_category_sort_key(category_key: str) -> tuple[int, str]:
    """Return the stable category order used by the VidAngel export UI."""
    normalized = str(category_key or "").strip()
    return (_UI_CATEGORY_ORDER_INDEX.get(normalized, len(_UI_CATEGORY_ORDER)), normalized)


def _censor_word(match: re.Match[str]) -> str:
    token = match.group(0)
    if token.lower() in _UNCENSORED_CONNECTORS or len(token) < 2:
        return token
    if len(token) == 2:
        return token
    return token[:2] + ("*" * (len(token) - 2))


def _display_label_for_category(category_key: str, raw_label: str, leaf_key: str) -> str:
    """Return a user-facing label while preserving raw keys for matching."""
    if not category_key.startswith("language_"):
        return raw_label
    language_label = leaf_key.replace("_", " ").title()
    return re.sub(r"[A-Za-z]+", _censor_word, language_label)


@lru_cache(maxsize=1)
def load_vidangel_taxonomy_records() -> list[dict[str, Any]]:
    """Return bundled VidAngel tag definitions from the repo-local export file."""
    source = _default_tag_definitions_path()
    if not source.exists():
        return []
    payload = json.loads(source.read_text(encoding="utf-8"))
    rows = payload.get("definitions")
    return [dict(row) for row in rows] if isinstance(rows, list) else []


@lru_cache(maxsize=1)
def build_vidangel_taxonomy() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Return category rows, label rows by category, and leaf lookup by leaf key."""
    records = load_vidangel_taxonomy_records()
    labels_by_category: dict[str, list[dict[str, Any]]] = {}
    leaf_lookup: dict[str, dict[str, Any]] = {}
    category_meta: dict[str, dict[str, Any]] = {}

    for record in records:
        path_keys = [str(part) for part in (record.get("path_keys") or []) if str(part).strip()]
        path_titles = [str(part) for part in (record.get("path_titles") or []) if str(part).strip()]
        category_key = normalize_vidangel_group_key(path_keys)
        leaf_key = str(record.get("key") or "").strip()
        if not category_key or not leaf_key:
            continue
        category_meta.setdefault(
            category_key,
            {
                "key": category_key,
                "label": _GROUP_LABEL_OVERRIDES.get(category_key, category_key.replace("_", " ").title()),
                "description": _GROUP_DESCRIPTION_OVERRIDES.get(category_key, "VidAngel filter category."),
                "default_threshold": 0.4 if category_key == "sex_nudity_immodesty" else 0.5,
            },
        )
        example = str(record.get("example_description") or "").strip()
        description = " > ".join(path_titles)
        if example:
            description = f"{description}. Example: {example}" if description else example
        label_row = {
            "key": leaf_key,
            "label": _display_label_for_category(
                category_key,
                str(record.get("display_title") or leaf_key),
                leaf_key,
            ),
            "description": description or "VidAngel filter label.",
            "default_skip": True,
            "default_detect": False,
        }
        labels_by_category.setdefault(category_key, []).append(label_row)
        leaf_lookup[leaf_key] = {
            "category": category_key,
            "record": dict(record),
            "label": dict(label_row),
        }

    category_rows = [
        {
            **meta,
            "labels": sorted(labels_by_category.get(meta["key"], []), key=lambda item: str(item["label"]).lower()),
        }
        for meta in sorted(category_meta.values(), key=lambda item: vidangel_ui_category_sort_key(item["key"]))
    ]
    return category_rows, labels_by_category, leaf_lookup


@lru_cache(maxsize=1)
def get_vidangel_category_rows() -> list[dict[str, Any]]:
    """Return VidAngel category metadata rows with exact leaf labels."""
    category_rows, _, _ = build_vidangel_taxonomy()
    return [dict(row) | {"labels": [dict(label) for label in row.get("labels", [])]} for row in category_rows]


@lru_cache(maxsize=1)
def get_vidangel_leaf_lookup() -> dict[str, dict[str, Any]]:
    """Return VidAngel taxonomy rows keyed by exact leaf key."""
    _, _, lookup = build_vidangel_taxonomy()
    return {key: dict(value) for key, value in lookup.items()}
