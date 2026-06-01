"""Leapfrog entry point — starts the watcher loops and web server."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn

from .logger import setup_logging, get_logger
from . import database as db
from .config import Config
from .paths import get_data_dir
from .server_runtime import get_client, init_active_client
from .watcher import session_watcher_loop, library_watcher_loop
from .scanner import scanner_loop
from .web.app import create_app
from .bg_jobs import recover_stale_jobs

DATA_DIR = get_data_dir()
logger = get_logger(__name__)


async def _amain() -> None:
    # ── Bootstrap ──────────────────────────────────────────────────────────
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.set_db_path(DATA_DIR / "leapfrog.db")
    await db.init_db()

    config = await Config.load()
    setup_logging(config.log_level, config.log_buffer_capacity)

    # Mark any jobs left in running/queued state from a previous crashed process.
    await recover_stale_jobs()

    logger.info("Leapfrog starting — data dir: %s", DATA_DIR)

    if config.is_configured():
        init_active_client(config)
        logger.info("%s client initialised: %s", config.server_type.capitalize(), config.server_url)
    else:
        logger.warning("Media server not yet configured — open the web UI to set up.")

    # ── Shared config factory ──────────────────────────────────────────────
    async def get_config():
        return await Config.load()

    # ── Web server ─────────────────────────────────────────────────────────
    web_app = create_app()
    host = os.environ.get("LEAPFROG_HOST", "0.0.0.0")
    port = int(os.environ.get("LEAPFROG_PORT", "7979"))

    config_uvicorn = uvicorn.Config(
        web_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config_uvicorn)

    logger.info("Web UI available at http://%s:%d", host, port)

    # ── Run all loops concurrently ─────────────────────────────────────────
    await asyncio.gather(
        server.serve(),
        session_watcher_loop(get_config, get_client),
        library_watcher_loop(get_config, get_client),
        scanner_loop(get_config),
    )


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
