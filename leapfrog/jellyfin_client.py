"""Jellyfin server API wrapper."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from .logger import get_logger
from .media_server import (
    ServerLibrarySection,
    ServerMediaItem,
    ServerSession,
    ServerUser,
)

logger = get_logger(__name__)


class JellyfinClient:
    """Minimal Jellyfin client for session polling, seek, users, and library sync."""

    adapter = "jellyfin"

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._library_title_cache: dict[str, str] = {}
        self._http = httpx.AsyncClient(
            base_url=self.url,
            timeout=15,
            headers={
                "Authorization": (
                    'MediaBrowser Client="Leapfrog", Device="Leapfrog", '
                    'DeviceId="leapfrog-server", Version="0.1.0", Token="%s"' % token
                ),
                "X-Emby-Token": token,
            },
        )
        self._last_seek_success_at: str | None = None
        self._last_seek_failure: dict[str, Any] | None = None
        self._last_seek_diagnostics: dict[str, Any] | None = None

    @staticmethod
    def _ticks_to_ms(value: Any) -> int:
        try:
            return int(int(value or 0) / 10_000)
        except Exception:
            return 0

    @staticmethod
    def _ms_to_ticks(value: int) -> int:
        return max(0, int(value)) * 10_000

    def _record_seek_success(self) -> None:
        self._last_seek_success_at = datetime.now().isoformat(timespec="seconds")

    def _record_seek_failure(self, session: ServerSession, detail: str, status_code: int | None = None) -> None:
        self._last_seek_failure = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "method": "jellyfin_seek",
            "client_identifier": session.client_identifier,
            "client_address": session.client_address,
            "client_port": session.client_port,
            "status_code": status_code,
            "detail": detail,
        }

    def get_last_seek_success_at(self) -> str | None:
        return self._last_seek_success_at

    def get_last_seek_failure(self) -> dict[str, Any] | None:
        return dict(self._last_seek_failure) if self._last_seek_failure else None

    def get_last_seek_diagnostics(self) -> dict[str, Any] | None:
        if not self._last_seek_diagnostics:
            return None
        return {
            **self._last_seek_diagnostics,
            "attempts": [dict(attempt) for attempt in self._last_seek_diagnostics.get("attempts", [])],
        }

    async def close(self) -> None:
        await self._http.aclose()

    async def test_connection(self) -> tuple[bool, str]:
        try:
            response = await self._http.get("/System/Info/Public")
            response.raise_for_status()
            payload = response.json()
            return True, str(payload.get("ServerName") or payload.get("Version") or "Jellyfin")
        except Exception as exc:
            return False, str(exc)

    async def get_machine_identifier(self) -> str:
        """Return the server id when available."""
        try:
            response = await self._http.get("/System/Info")
            response.raise_for_status()
            return str(response.json().get("Id") or "")
        except Exception as exc:
            logger.debug("Failed to get Jellyfin server identifier: %s", exc)
            return ""

    async def get_active_sessions(self) -> list[ServerSession]:
        try:
            response = await self._http.get("/Sessions")
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Failed to fetch Jellyfin sessions: %s", exc)
            return []

        sessions: list[ServerSession] = []
        for raw in payload:
            now_playing = raw.get("NowPlayingItem") or {}
            play_state = raw.get("PlayState") or {}
            user_name = str(raw.get("UserName") or "Unknown")
            item_id = str(now_playing.get("Id") or "")
            item_type = str(now_playing.get("Type") or "").lower()
            series_name = str(now_playing.get("SeriesName") or "").strip()
            season_name = str(now_playing.get("SeasonName") or "").strip()
            episode_title = str(now_playing.get("Name") or raw.get("NowPlayingItemName") or "").strip()
            full_title = episode_title
            if item_type == "episode" and series_name:
                segments = [series_name]
                if season_name:
                    segments.append(season_name)
                if episode_title:
                    segments.append(episode_title)
                full_title = " – ".join(segments)
            image_tag = str((now_playing.get("ImageTags") or {}).get("Primary") or "")
            thumb = ""
            if item_id and image_tag:
                thumb = f"/Items/{item_id}/Images/Primary?tag={quote(image_tag, safe='')}"
            sessions.append(
                ServerSession(
                    adapter=self.adapter,
                    session_key=str(raw.get("Id") or ""),
                    user=user_name,
                    title=episode_title or str(now_playing.get("Name") or ""),
                    full_title=full_title or episode_title or str(now_playing.get("Name") or ""),
                    media_id=item_id,
                    rating_key=item_id,
                    media_type=item_type or "movie",
                    position_ms=self._ticks_to_ms(play_state.get("PositionTicks")),
                    duration_ms=self._ticks_to_ms(now_playing.get("RunTimeTicks")),
                    client_identifier=str(raw.get("DeviceId") or raw.get("Id") or ""),
                    client_title=str(raw.get("DeviceName") or raw.get("Client") or "Jellyfin Client"),
                    is_controllable=bool(raw.get("SupportsRemoteControl", False)),
                    thumb=thumb,
                    client_address=str(raw.get("RemoteEndPoint") or ""),
                    client_port=0,
                    library_section_id=str(now_playing.get("ParentId") or ""),
                    file_path=str(now_playing.get("Path") or ""),
                    external_ids={"jellyfin_item_id": item_id},
                )
            )
        return sessions

    async def seek(self, session: ServerSession, offset_ms: int) -> bool:
        self._last_seek_diagnostics = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "client_identifier": session.client_identifier,
            "offset_ms": offset_ms,
            "attempts": [],
            "success": False,
        }
        if not session.session_key:
            detail = "Missing Jellyfin session id"
            self._last_seek_diagnostics["attempts"].append({"method": "jellyfin_seek", "ok": False, "detail": detail})
            self._record_seek_failure(session, detail)
            return False
        try:
            response = await self._http.post(
                f"/Sessions/{quote(session.session_key, safe='')}/Playing/Seek",
                params={"SeekPositionTicks": self._ms_to_ticks(offset_ms)},
            )
            ok = response.status_code < 300
            detail = "Seek accepted" if ok else (response.text[:500] or f"HTTP {response.status_code}")
            self._last_seek_diagnostics["attempts"].append(
                {"method": "jellyfin_seek", "ok": ok, "detail": detail, "status_code": response.status_code}
            )
            self._last_seek_diagnostics["success"] = ok
            if ok:
                self._record_seek_success()
                return True
            self._record_seek_failure(session, detail, status_code=response.status_code)
            return False
        except Exception as exc:
            detail = str(exc)
            self._last_seek_diagnostics["attempts"].append({"method": "jellyfin_seek", "ok": False, "detail": detail})
            self._record_seek_failure(session, detail)
            return False

    async def get_library_sections(self) -> list[ServerLibrarySection]:
        try:
            response = await self._http.get("/Library/VirtualFolders")
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Failed to fetch Jellyfin libraries: %s", exc)
            return []

        sections: list[ServerLibrarySection] = []
        for row in payload:
            collection_type = str(row.get("CollectionType") or "")
            if collection_type not in {"movies", "tvshows"}:
                continue
            item_type = "movie" if collection_type == "movies" else "show"
            section_id = str(row.get("ItemId") or row.get("Id") or "")
            title = str(row.get("Name") or "")
            self._library_title_cache[section_id] = title
            sections.append(
                ServerLibrarySection(
                    section_id=section_id,
                    title=title,
                    section_type=item_type,
                )
            )
        return sections

    async def _get_library_title(self, section_id: str) -> str:
        """Resolve one Jellyfin section id back to its library title."""
        if section_id in self._library_title_cache:
            return self._library_title_cache[section_id]
        for section in await self.get_library_sections():
            if section.section_id == section_id:
                return section.title
        return ""

    async def get_library_items(self, section_id: str) -> list[ServerMediaItem]:
        library_title = await self._get_library_title(section_id)
        try:
            response = await self._http.get(
                "/Items",
                params={
                    "ParentId": section_id,
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Episode",
                    "Fields": "Path,ProviderIds,ProductionYear,OfficialRating,SeriesId,ImageTags",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Failed to fetch Jellyfin library items for %s: %s", section_id, exc)
            return []

        items: list[ServerMediaItem] = []
        for raw in payload.get("Items", []):
            item_id = str(raw.get("Id") or "")
            item_type = str(raw.get("Type") or "").lower()
            image_tag = str((raw.get("ImageTags") or {}).get("Primary") or "")
            thumb = f"/Items/{item_id}/Images/Primary?tag={quote(image_tag, safe='')}" if item_id and image_tag else ""
            provider_ids = raw.get("ProviderIds") or {}
            external_ids = {f"provider_{key.lower()}": str(value) for key, value in provider_ids.items() if value}
            external_ids["jellyfin_item_id"] = item_id
            items.append(
                ServerMediaItem(
                    media_id=item_id,
                    title=str(raw.get("Name") or ""),
                    year=int(raw["ProductionYear"]) if raw.get("ProductionYear") is not None else None,
                    thumb=thumb,
                    file_path=str(raw.get("Path") or ""),
                    library_id=section_id,
                    library_title=library_title,
                    media_type="episode" if item_type == "episode" else "movie",
                    content_rating=str(raw.get("OfficialRating") or ""),
                    show_guid=str(raw.get("SeriesId") or ""),
                    rating_key=item_id,
                    external_ids=external_ids,
                )
            )
        return items

    async def get_all_users(self) -> list[ServerUser]:
        try:
            response = await self._http.get("/Users")
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Failed to fetch Jellyfin users: %s", exc)
            return []

        users: list[ServerUser] = []
        for row in payload:
            user_id = str(row.get("Id") or "")
            thumb = f"/Users/{quote(user_id, safe='')}/Images/Primary" if user_id else ""
            users.append(
                ServerUser(
                    username=str(row.get("Name") or "Unknown"),
                    thumb=thumb,
                    user_id=user_id,
                )
            )
        return users

    def build_image_url(self, image_ref: str) -> str:
        """Route Jellyfin artwork through Leapfrog so browser requests stay authenticated."""
        if not image_ref:
            return ""
        if image_ref.startswith("http://") or image_ref.startswith("https://"):
            return image_ref
        normalized_ref = image_ref if image_ref.startswith("/") else f"/{image_ref}"
        return f"/api/server-image?ref={quote(normalized_ref, safe='')}"

    async def fetch_image(self, image_ref: str) -> tuple[bytes, str]:
        if not image_ref:
            return b"", ""
        try:
            response = await self._http.get(image_ref if image_ref.startswith("/") else f"/{image_ref}")
            if response.status_code >= 400:
                return b"", ""
            return response.content, response.headers.get("content-type", "image/jpeg")
        except Exception as exc:
            logger.debug("Failed to fetch Jellyfin image %s: %s", image_ref, exc)
            return b"", ""
