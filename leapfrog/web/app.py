"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .. import __version__
from .routes.settings import router as settings_router
from .routes.status import router as status_router
from .routes.sessions import router as sessions_router
from .routes.users import router as users_router
from .routes.segments import router as segments_router
from .routes.scanner_routes import router as scanner_router
from .routes.thumbnails import router as thumbnails_router
from .routes.sync_routes import router as sync_router

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Leapfrog",
        description="Server-side media filtering for Plex libraries",
        version=__version__,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(settings_router)
    app.include_router(status_router)
    app.include_router(sessions_router)
    app.include_router(users_router)
    app.include_router(segments_router)
    app.include_router(scanner_router)
    app.include_router(thumbnails_router)
    app.include_router(sync_router)

    # Serve built React frontend (if present)
    if STATIC_DIR.exists():
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/", include_in_schema=False)
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str = ""):
            index = STATIC_DIR / "index.html"
            if index.exists():
                return FileResponse(str(index))
            return {"message": "Leapfrog API running. Frontend not built yet."}

    return app
