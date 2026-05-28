"""Unit tests for VidAngel-first category metadata."""

from __future__ import annotations

import pytest

from leapfrog.preferences import get_category_metadata


@pytest.mark.asyncio
async def test_get_category_metadata_uses_vidangel_taxonomy():
    categories = await get_category_metadata()
    category_keys = {category["key"] for category in categories}

    assert "language_profanity" in category_keys
    assert "sex_nudity_immodesty" in category_keys

    profanity = next(category for category in categories if category["key"] == "language_profanity")
    label_keys = {label["key"] for label in profanity["labels"]}

    assert "fuck" in label_keys
    assert "shit" in label_keys
    assert next(label for label in profanity["labels"] if label["key"] == "fuck")["label"] == "Fu**"
    assert next(label for label in profanity["labels"] if label["key"] == "shit")["label"] == "Sh**"
