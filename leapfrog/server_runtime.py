"""Active media-server client creation and lifecycle helpers."""

from __future__ import annotations

from .config import Config
from .jellyfin_client import JellyfinClient
from .media_server import clear_client, get_client, init_client
from .plex_client import PlexClient


def build_client(server_type: str, url: str, token: str):
    """Create a concrete media-server client for the configured server type."""
    normalized = (server_type or "plex").strip().lower()
    if normalized == "jellyfin":
        return JellyfinClient(url, token)
    return PlexClient(url, token)


def init_active_client(config: Config):
    """Initialize and register the active server client for the given config."""
    client = build_client(config.server_type, config.server_url, config.server_token)
    return init_client(client)


__all__ = ["build_client", "clear_client", "get_client", "init_active_client"]
