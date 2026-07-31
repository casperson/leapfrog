"""Web API routes for VidAngel export browsing and filtered SKP generation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ...logger import get_logger
from ...vidangel_export import (
    build_vidangel_filter_catalog,
    generate_filtered_vidangel_skp,
    get_vidangel_export_dir,
    prepare_vidangel_export,
    VidAngelExportDataError,
)
from ...vidangel_sidecars import load_vidangel_library_title_catalog_records

logger = get_logger(__name__)
router = APIRouter(prefix="/api/vidangel", tags=["vidangel"])


class GenerateVidAngelSidecarsRequest(BaseModel):
    library_id: str | None = None
    dry_run: bool = False
    overwrite: bool = False


class GenerateVidAngelSkpRequest(BaseModel):
    selected_leaf_keys: list[str] = Field(default_factory=list)
    selected_event_ids: list[str] | None = None
    output_path: str | None = None


@router.post("/sidecars/generate")
async def generate_sidecars(payload: GenerateVidAngelSidecarsRequest):
    """Generate VidAngel sidecars for all or one Plex library."""
    from ...vidangel_sidecars import generate_vidangel_sidecars

    return await generate_vidangel_sidecars(
        library_id=payload.library_id,
        only_missing=not payload.overwrite,
        dry_run=payload.dry_run,
    )


@router.get("/export/catalog")
async def get_export_catalog():
    """Return exportable VidAngel titles matched to the local media library."""
    try:
        titles = await load_vidangel_library_title_catalog_records()
    except VidAngelExportDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "available": bool(titles),
        "export_dir": str(get_vidangel_export_dir()) if titles else "",
        "title_count": len(titles),
        "titles": titles,
    }


@router.post("/export/prepare")
async def prepare_existing_export():
    """Index and warm existing VidAngel export files without refetching them."""
    try:
        return await prepare_vidangel_export()
    except VidAngelExportDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/export/titles/{media_id}/filters")
async def get_title_filters(media_id: str):
    """Return the available leaf filters for one VidAngel title."""
    try:
        return await build_vidangel_filter_catalog(media_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="VidAngel title not found")
    except VidAngelExportDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/export/titles/{media_id}/skp")
async def generate_title_skp(media_id: str, payload: GenerateVidAngelSkpRequest):
    """Generate native Clean Media Player .skp JSON for one VidAngel title."""
    try:
        result = await generate_filtered_vidangel_skp(
            media_id,
            payload.selected_leaf_keys,
            output_path=payload.output_path,
            selected_event_ids=payload.selected_event_ids,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="VidAngel title not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except VidAngelExportDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if payload.output_path:
        return {
            "ok": True,
            "filename": result["filename"],
            "output_path": result["output_path"],
            "selected_event_count": result["selected_event_count"],
            "selected_leaf_keys": result["selected_leaf_keys"],
            "selected_event_ids": result["selected_event_ids"],
            "title": result["title"],
        }

    headers = {
        "Content-Disposition": f'attachment; filename="{result["filename"]}"',
    }
    return Response(content=result["skp_text"], media_type="application/json", headers=headers)
