from fastapi import APIRouter
from pydantic import BaseModel

from ...domain import PREFERENCE_CATEGORIES
from ...logger import get_logger
from ...preferences import (
    build_user_category_preferences_map,
    build_user_filter_map,
    build_user_label_preferences_map,
    get_category_metadata,
    get_default_skip_label_settings,
    get_preference_threshold_settings,
    resolve_preferences_for_users,
)
import leapfrog.plex_client as plex_mod
from ... import database as db

logger = get_logger(__name__)
router = APIRouter(prefix="/api/users", tags=["users"])


class UserFilterUpdate(BaseModel):
    enabled: bool


class UserCategoryPreferenceUpdate(BaseModel):
    enabled: bool
    threshold: float | None = None


class UserLabelPreferenceUpdate(BaseModel):
    enabled: bool
    threshold: float | None = None


@router.get("")
async def get_users():
    """Return all Plex users merged with their filter settings."""
    # Get users from Plex if available
    plex_users: list[dict] = []
    try:
        client = plex_mod.get_client()
        users = await client.get_all_users()
        plex_users = [{"username": u.username, "thumb": u.thumb} for u in users]
    except RuntimeError:
        pass

    # Get DB filter settings
    filters = build_user_filter_map(await db.get_all_user_filters())
    category_preferences = build_user_category_preferences_map(
        await db.get_all_user_category_preferences()
    )
    label_preferences = build_user_label_preferences_map(
        await db.get_all_user_label_preferences()
    )
    threshold_defaults = await get_preference_threshold_settings()
    default_skip_labels = await get_default_skip_label_settings()
    category_metadata = await get_category_metadata()
    label_definitions_by_category = {
        str(category["key"]): [dict(label) for label in category.get("labels", [])]
        for category in category_metadata
    }
    known_usernames = sorted(
        set(filters) | set(category_preferences) | set(label_preferences) | {u["username"] for u in plex_users}
    )
    resolved_preferences = resolve_preferences_for_users(
        known_usernames,
        overall_filters=filters,
        stored_preferences_by_user=category_preferences,
        threshold_defaults=threshold_defaults,
        stored_label_preferences_by_user=label_preferences,
        label_definitions_by_category=label_definitions_by_category,
        default_skip_labels=default_skip_labels,
    )

    # Merge: if username not in DB, default enabled=True
    result = []
    seen = set()
    for u in plex_users:
        name = u["username"]
        seen.add(name)
        result.append({
            "username": name,
            "thumb": u.get("thumb", ""),
            "enabled": filters.get(name, True),
            "categories": resolved_preferences.get(name, {}),
        })

    # Also include any DB entries not returned by Plex
    for name in known_usernames:
        enabled = filters.get(name, True)
        if name not in seen:
            result.append({
                "username": name,
                "thumb": "",
                "enabled": enabled,
                "categories": resolved_preferences.get(name, {}),
            })

    return {"users": result}


@router.put("/{username}")
async def update_user_filter(username: str, payload: UserFilterUpdate):
    await db.upsert_user_filter(username, payload.enabled)
    return {"ok": True}


@router.put("/{username}/categories/{category}")
async def update_user_category_preference(
    username: str,
    category: str,
    payload: UserCategoryPreferenceUpdate,
):
    if category not in PREFERENCE_CATEGORIES:
        return {"ok": False, "error": f"Unsupported category: {category}"}
    await db.upsert_user_category_preference(
        username,
        category,
        enabled=payload.enabled,
        threshold=payload.threshold,
    )
    return {"ok": True}


@router.put("/{username}/categories/{category}/labels/{label}")
async def update_user_label_preference(
    username: str,
    category: str,
    label: str,
    payload: UserLabelPreferenceUpdate,
):
    if category not in PREFERENCE_CATEGORIES:
        return {"ok": False, "error": f"Unsupported category: {category}"}
    category_metadata = await get_category_metadata()
    valid_labels = {
        str(label.get("key"))
        for category_row in category_metadata
        if str(category_row.get("key")) == category
        for label in category_row.get("labels", [])
    }
    if label not in valid_labels:
        return {"ok": False, "error": f"Unsupported label for {category}: {label}"}
    await db.set_user_label_preference(
        username,
        category,
        label,
        enabled=payload.enabled,
        threshold=payload.threshold,
    )
    return {"ok": True}
