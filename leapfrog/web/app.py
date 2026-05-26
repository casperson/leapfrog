"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from .routes.settings import router as settings_router
from .routes.status import router as status_router
from .routes.sessions import router as sessions_router
from .routes.users import router as users_router
from .routes.segments import router as segments_router
from .routes.scanner_routes import router as scanner_router
from .routes.thumbnails import router as thumbnails_router
from .routes.sync_routes import router as sync_router
from .routes.logs import router as logs_router
from .routes.vidangel_routes import router as vidangel_router

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
    app.include_router(logs_router)
    app.include_router(vidangel_router)

    @app.api_route("/api/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"], include_in_schema=False)
    async def api_not_found(full_path: str):
        """Return API 404s explicitly so the SPA catch-all never masks them as 405s."""
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

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
