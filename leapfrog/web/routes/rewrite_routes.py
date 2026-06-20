"""Web API routes for VidAngel-taxonomy media rewrite exports."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...bg_jobs import enqueue_rewrite_job, get_job_status
from ...logger import get_logger
from ... import media_rewriter

logger = get_logger(__name__)
router = APIRouter(prefix="/api/rewrite", tags=["rewrite"])


class RewriteSettingsPayload(BaseModel):
    selected_leaf_keys: list[str] | None = None
    language_mode: str | None = None
    profile_user: str | None = None


class RewritePlanRequest(BaseModel):
    media_ids: list[str] = Field(default_factory=list)
    selected_leaf_keys: list[str] = Field(default_factory=list)
    profile_user: str | None = None
    language_mode: str | None = None
    per_media_language_mode: dict[str, str] = Field(default_factory=dict)


class RewriteJobsRequest(RewritePlanRequest):
    size_limit_percent: int | None = None


@router.get("/settings")
async def get_rewrite_settings():
    """Return persisted rewrite defaults."""
    return await media_rewriter.get_rewrite_settings()


@router.put("/settings")
async def update_rewrite_settings(payload: RewriteSettingsPayload):
    """Update persisted rewrite defaults."""
    return await media_rewriter.update_rewrite_settings(
        selected_leaf_keys=payload.selected_leaf_keys,
        language_mode=payload.language_mode,
        profile_user=payload.profile_user,
    )


@router.get("/candidates")
async def get_rewrite_candidates():
    """Return media titles and rewrite readiness metadata."""
    settings = await media_rewriter.get_rewrite_settings()
    return {
        "settings": settings,
        "candidates": await media_rewriter.discover_rewrite_candidates(),
    }


@router.get("/profile-leaf-keys/{user_id}")
async def get_profile_leaf_keys(user_id: str):
    """Return resolved VidAngel leaf keys for one saved user profile."""
    return {
        "user_id": user_id,
        "selected_leaf_keys": await media_rewriter.get_leaf_keys_for_user_profile(user_id),
    }


@router.post("/plan")
async def preview_rewrite_plan(payload: RewritePlanRequest):
    """Return dry-run rewrite previews for the requested titles."""
    media_ids = [str(value).strip() for value in payload.media_ids if str(value).strip()]
    if not media_ids:
        raise HTTPException(status_code=400, detail="At least one media id is required.")

    selected_leaf_keys = [str(value).strip() for value in payload.selected_leaf_keys if str(value).strip()]
    if not selected_leaf_keys and payload.profile_user:
        selected_leaf_keys = await media_rewriter.get_leaf_keys_for_user_profile(payload.profile_user)
    if not selected_leaf_keys:
        raise HTTPException(status_code=400, detail="At least one VidAngel leaf filter must be selected.")

    language_mode = str(payload.language_mode or media_rewriter.REWRITE_LANGUAGE_MODE_DEFAULT)
    plans = []
    for media_id in media_ids:
        try:
            plans.append(
                await media_rewriter.build_rewrite_plan(
                    media_id,
                    selected_leaf_keys=selected_leaf_keys,
                    language_mode=str(payload.per_media_language_mode.get(media_id) or language_mode),
                )
            )
        except media_rewriter.RewriteEligibilityError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except media_rewriter.RewriteExecutionError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
    return {
        "selected_leaf_keys": selected_leaf_keys,
        "profile_user": payload.profile_user,
        "language_mode": language_mode,
        "plans": plans,
    }


@router.post("/jobs")
async def start_rewrite_jobs(payload: RewriteJobsRequest):
    """Start one or more media rewrite exports in the background."""
    media_ids = [str(value).strip() for value in payload.media_ids if str(value).strip()]
    if not media_ids:
        raise HTTPException(status_code=400, detail="At least one media id is required.")
    if not payload.selected_leaf_keys and not payload.profile_user:
        raise HTTPException(status_code=400, detail="At least one VidAngel leaf filter must be selected.")
    job_id = await enqueue_rewrite_job(payload.model_dump())
    return {
        "status": "running",
        "job_id": job_id,
    }


@router.get("/jobs/{job_id}")
async def get_rewrite_job(job_id: int):
    """Return the current status for one rewrite background job."""
    job = await get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job
