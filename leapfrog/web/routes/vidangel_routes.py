"""Web API routes for VidAngel sidecar generation."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ...vidangel_sidecars import generate_vidangel_sidecars

router = APIRouter(prefix="/api/vidangel", tags=["vidangel"])


class GenerateVidAngelSidecarsRequest(BaseModel):
    library_id: str | None = None
    dry_run: bool = False
    overwrite: bool = False


@router.post("/sidecars/generate")
async def generate_sidecars(payload: GenerateVidAngelSidecarsRequest):
    """Generate VidAngel sidecars for all or one Plex library."""
    return await generate_vidangel_sidecars(
        library_id=payload.library_id,
        only_missing=not payload.overwrite,
        dry_run=payload.dry_run,
    )
