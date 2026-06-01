"""Canonical media-server models and active client registry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(slots=True)
class ServerSession:
    """One active playback session resolved into canonical fields."""

    adapter: str
    session_key: str
    user: str
    title: str
    full_title: str
    media_id: str
    rating_key: str
    media_type: str
    position_ms: int
    duration_ms: int
    client_identifier: str
    client_title: str
    is_controllable: bool
    thumb: str = ""
    client_address: str = ""
    client_port: int = 0
    library_section_id: str = ""
    file_path: str = ""
    external_ids: dict[str, str] | None = None


@dataclass(slots=True)
class ServerLibrarySection:
    """A browseable server library section."""

    section_id: str
    title: str
    section_type: str


@dataclass(slots=True)
class ServerMediaItem:
    """A canonical media item for scanning and segment lookup."""

    media_id: str
    title: str
    year: int | None
    thumb: str
    file_path: str
    library_id: str
    library_title: str
    media_type: str
    content_rating: str = ""
    show_guid: str = ""
    rating_key: str = ""
    external_ids: dict[str, str] | None = None


@dataclass(slots=True)
class ServerUser:
    """A server user/profile record."""

    username: str
    thumb: str = ""
    user_id: str = ""


class MediaServerClient(Protocol):
    """Shared interface for Plex- and Jellyfin-like server clients."""

    adapter: str

    async def test_connection(self) -> tuple[bool, str]:
        """Return whether the server is reachable and a status detail."""

    async def get_active_sessions(self) -> list[ServerSession]:
        """Return currently active playback sessions."""

    async def seek(
        self,
        session: ServerSession,
        offset_ms: int,
    ) -> bool:
        """Seek one active session to a target position."""

    async def get_library_sections(self) -> list[ServerLibrarySection]:
        """Return library sections eligible for sync/scanning."""

    async def get_library_items(self, section_id: str) -> list[ServerMediaItem]:
        """Return media items for one library section."""

    async def get_all_users(self) -> list[ServerUser]:
        """Return users known to the server."""

    async def get_machine_identifier(self) -> str:
        """Return a stable server identifier when available."""

    def build_image_url(self, image_ref: str) -> str:
        """Return a directly fetchable image URL when possible."""

    async def fetch_image(self, image_ref: str) -> tuple[bytes, str]:
        """Fetch one image asset by server-native reference."""

    async def close(self) -> None:
        """Close any underlying network resources."""

    def get_last_seek_failure(self) -> dict[str, Any] | None:
        """Return the last seek failure if available."""

    def get_last_seek_success_at(self) -> str | None:
        """Return the last successful seek timestamp if available."""

    def get_last_seek_diagnostics(self) -> dict[str, Any] | None:
        """Return the latest seek diagnostics if available."""


_client: MediaServerClient | None = None


def init_client(client: MediaServerClient) -> MediaServerClient:
    """Store the active server client singleton."""
    global _client
    if _client is not None:
        # Close the previous client's pooled connections before swapping adapters.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_client.close())
        except RuntimeError:
            pass  # no running loop — process is tearing down
    _client = client
    return client


def clear_client() -> None:
    """Forget the active server client singleton."""
    global _client
    if _client is not None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_client.close())
        except RuntimeError:
            pass  # no running loop — process is tearing down
    _client = None


def get_client() -> MediaServerClient:
    """Return the active server client singleton."""
    if _client is None:
        raise RuntimeError("Media server client not initialised")
    return _client
