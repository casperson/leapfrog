"""Plex Media Server API wrapper."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# TTL for per-show metadata cache; 10 minutes is long enough to cover a full
# library-titles request without stale data causing visible issues.
_SHOW_ART_CACHE_TTL_S = 600

import httpx
from plexapi.server import PlexServer
from plexapi.exceptions import PlexApiException

from .logger import get_logger

logger = get_logger(__name__)


@dataclass
class ActiveSession:
    session_key: str
    user: str
    title: str
    full_title: str
    plex_guid: str
    rating_key: str
    media_type: str       # "movie" or "episode"
    position_ms: int
    duration_ms: int
    client_identifier: str
    client_title: str
    is_controllable: bool
    thumb: str = ""       # relative Plex thumb URL
    client_address: str = ""
    client_port: int = 32500
    library_section_id: str = ""
    file_path: str = ""


@dataclass
class LibrarySection:
    section_id: str
    title: str
    section_type: str     # "movie" or "show"


@dataclass
class MediaItem:
    rating_key: str
    plex_guid: str
    title: str
    year: int | None
    thumb: str
    file_path: str
    library_id: str
    library_title: str
    media_type: str       # "movie" or "episode"
    content_rating: str = ""  # e.g. "PG-13", "R", "TV-MA"
    show_guid: str = ""       # grandparentGuid for episodes; empty for movies


@dataclass
class PlexUser:
    username: str
    thumb: str = ""
    is_home_user: bool = True


class PlexClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._server: PlexServer | None = None
        self._http = httpx.AsyncClient(timeout=10)
        # {rating_key: (monotonic_timestamp, (show_guid, show_title, show_thumb, show_rating_key, season_rating_key))}
        self._show_art_cache: dict[str, tuple[float, tuple[str, str, str, str, str]]] = {}
        self._last_seek_success_at: str | None = None
        self._last_seek_failure: dict[str, Any] | None = None
        self._last_seek_diagnostics: dict[str, Any] | None = None
        self._command_id = int(time.time() * 1000)

    def _next_command_id(self) -> int:
        self._command_id += 1
        return self._command_id

    @staticmethod
    def _redact_token(value: str) -> str:
        return re.sub(r"([?&]X-Plex-Token=)[^&]+", r"\1<redacted>", value)

    def _record_seek_success(self) -> None:
        self._last_seek_success_at = datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _is_loopback_client_address(client_address: str) -> bool:
        normalized = (client_address or "").strip().lower().strip("[]")
        return normalized == "localhost" or normalized == "::1" or normalized.startswith("127.")

    @staticmethod
    def _format_client_address_for_url(client_address: str) -> str:
        normalized = (client_address or "").strip()
        if ":" in normalized and not normalized.startswith("["):
            return f"[{normalized}]"
        return normalized

    @staticmethod
    def _is_browser_client_title(client_title: str) -> bool:
        normalized = (client_title or "").strip().lower()
        return normalized in {"chrome", "firefox", "safari", "edge", "opera", "brave", "plex web"}

    def _start_seek_diagnostics(self, *, client_identifier: str, offset_ms: int) -> dict[str, Any]:
        self._last_seek_diagnostics = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "client_identifier": client_identifier,
            "offset_ms": offset_ms,
            "attempts": [],
            "success": False,
        }
        return self._last_seek_diagnostics

    def _record_seek_attempt(
        self,
        *,
        method: str,
        ok: bool,
        detail: str,
        target: str = "",
        client_address: str = "",
        client_port: int | None = None,
        status_code: int | None = None,
        variant: int | None = None,
    ) -> None:
        if self._last_seek_diagnostics is None:
            return
        self._last_seek_diagnostics["attempts"].append({
            "at": datetime.now().isoformat(timespec="seconds"),
            "method": method,
            "ok": ok,
            "target": self._redact_token(target),
            "client_address": client_address,
            "client_port": client_port,
            "status_code": status_code,
            "variant": variant,
            "detail": detail,
        })
        self._last_seek_diagnostics["success"] = bool(self._last_seek_diagnostics["success"] or ok)

    def _record_seek_failure(
        self,
        *,
        method: str,
        client_identifier: str,
        detail: str,
        client_address: str = "",
        client_port: int | None = None,
        status_code: int | None = None,
        variant: int | None = None,
    ) -> None:
        self._last_seek_failure = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "method": method,
            "client_identifier": client_identifier,
            "client_address": client_address,
            "client_port": client_port,
            "status_code": status_code,
            "variant": variant,
            "detail": detail,
        }

    async def _resolve_companion_client(self, client_identifier: str) -> tuple[Any | None, str]:
        """Return a discovered plexapi client and a short diagnostics summary."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            clients = await asyncio.to_thread(srv.clients)
        except Exception as exc:
            return None, f"Companion discovery failed: {exc}"

        for client in clients:
            if getattr(client, "machineIdentifier", "") != client_identifier:
                continue
            capabilities = ",".join(getattr(client, "protocolCapabilities", []) or [])
            baseurl = getattr(client, "_baseurl", "") or getattr(client, "address", "") or ""
            product = getattr(client, "product", "") or getattr(client, "title", "") or "unknown"
            detail = f"baseurl={baseurl or '<unknown>'}; product={product}; capabilities={capabilities or '<none>'}"
            return client, detail
        return None, "Companion client not found in /clients discovery list"

    async def _seek_via_companion_client_proxy(self, client: Any, offset_ms: int) -> tuple[bool, str]:
        """Try PMS-proxied seek using a discovered plexapi client."""
        try:
            await asyncio.to_thread(client.proxyThroughServer, True)
            await asyncio.to_thread(client.seekTo, offset_ms)
            return True, "Discovered Companion client proxy seek accepted"
        except Exception as exc:
            return False, str(exc)

    async def _seek_via_companion_client_direct(self, client: Any, offset_ms: int) -> tuple[bool, str]:
        """Try direct seek using a discovered plexapi client base URL."""
        try:
            await asyncio.to_thread(client.proxyThroughServer, False)
            await asyncio.to_thread(client.connect)
            await asyncio.to_thread(client.seekTo, offset_ms)
            return True, "Discovered Companion client direct seek accepted"
        except Exception as exc:
            return False, str(exc)

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

    async def list_companion_clients(self) -> list[dict[str, Any]]:
        """Return Companion-discovered Plex clients from /clients with key metadata."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            clients = await asyncio.to_thread(srv.clients)
        except Exception as exc:
            logger.warning("Failed to fetch Companion clients: %s", exc)
            return []

        result: list[dict[str, Any]] = []
        for client in clients:
            result.append({
                "title": getattr(client, "title", "") or "",
                "machine_identifier": getattr(client, "machineIdentifier", "") or "",
                "product": getattr(client, "product", "") or "",
                "protocol": getattr(client, "protocol", "") or "",
                "protocol_version": getattr(client, "protocolVersion", "") or "",
                "device_class": getattr(client, "deviceClass", "") or "",
                "protocol_capabilities": list(getattr(client, "protocolCapabilities", []) or []),
                "address": getattr(client, "address", "") or "",
                "port": int(getattr(client, "port", 0) or 0),
                "baseurl": getattr(client, "_baseurl", "") or "",
            })
        return result

    def _get_server(self) -> PlexServer:
        if self._server is None:
            self._server = PlexServer(self.url, self.token)
        return self._server

    def invalidate(self) -> None:
        self._server = None

    @staticmethod
    def resolve_probe_offset(current_position_ms: int, *, offset_ms: int | None = None, delta_ms: int = 1000) -> int:
        """Return an explicit probe target offset for an active playback session."""
        if offset_ms is not None:
            return max(0, int(offset_ms))
        return max(0, int(current_position_ms) + max(0, int(delta_ms)))

    # ── Connectivity ──────────────────────────────────────────────────────────

    async def test_connection(self) -> tuple[bool, str]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            return True, srv.friendlyName
        except Exception as exc:
            return False, str(exc)

    async def get_machine_identifier(self) -> str:
        """Return the Plex server machine identifier, used to build web deep links."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            return str(srv.machineIdentifier)
        except Exception as exc:
            logger.debug("Failed to get machine identifier: %s", exc)
            return ""

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def get_active_sessions(self) -> list[ActiveSession]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            sessions = await asyncio.to_thread(srv.sessions)
        except Exception as exc:
            logger.warning("Failed to fetch sessions: %s", exc)
            return []

        result = []
        for s in sessions:
            try:
                # Determine full title
                if hasattr(s, "grandparentTitle") and s.grandparentTitle:
                    full_title = f"{s.grandparentTitle} – {s.parentTitle} – {s.title}"
                else:
                    full_title = s.title

                # Find the first player
                player = s.players[0] if s.players else None
                client_id = player.machineIdentifier if player else ""
                client_title = player.title if player else "Unknown"
                controllable = bool(player and player.state is not None) if player else False
                client_address = getattr(player, "address", "") if player else ""
                client_port_raw = getattr(player, "port", 32500) if player else 32500
                try:
                    client_port = int(client_port_raw or 32500)
                except Exception:
                    client_port = 32500

                # Resolve file path
                file_path = ""
                if s.media and s.media[0].parts:
                    file_path = s.media[0].parts[0].file or ""

                # GUID — prefer the first one that looks useful
                guid = ""
                if hasattr(s, "guids") and s.guids:
                    guid = s.guids[0].id
                elif hasattr(s, "guid"):
                    guid = s.guid or ""

                section_id = str(s.librarySectionID) if hasattr(s, "librarySectionID") else ""

                result.append(
                    ActiveSession(
                        session_key=str(s.sessionKey),
                        user=s.usernames[0] if s.usernames else "Unknown",
                        title=s.title,
                        full_title=full_title,
                        plex_guid=guid,
                        rating_key=str(s.ratingKey),
                        media_type=s.type,
                        position_ms=int(s.viewOffset or 0),
                        duration_ms=int(s.duration or 0),
                        client_identifier=client_id,
                        client_title=client_title,
                        is_controllable=controllable,
                        client_address=client_address,
                        client_port=client_port,
                        thumb=s.thumb or "",
                        library_section_id=section_id,
                        file_path=file_path,
                    )
                )
            except Exception as exc:
                logger.debug("Error parsing session: %s", exc)

        return result

    # ── Seek ──────────────────────────────────────────────────────────────────

    async def seek(
        self,
        client_identifier: str,
        offset_ms: int,
        client_address: str = "",
        client_port: int = 32500,
        client_title: str = "",
    ) -> bool:
        """Seek via server proxy first, then try direct client control as fallback."""
        command_id = self._next_command_id()
        self._start_seek_diagnostics(client_identifier=client_identifier, offset_ms=offset_ms)
        key = (
            f"/player/playback/seekTo"
            f"?offset={offset_ms}"
            f"&type=video"
            f"&commandID={command_id}"
        )
        try:
            srv = await asyncio.to_thread(self._get_server)
            headers = {
                "X-Plex-Target-Client-Identifier": client_identifier,
                "X-Plex-Client-Identifier": "leapfrog-server",
                "X-Plex-Product": "Leapfrog",
                "X-Plex-Device-Name": "Leapfrog",
                "X-Plex-Platform": "Windows",
            }
            await asyncio.to_thread(srv.query, key, headers=headers)
            logger.info("Seeked client %s to %dms via server query proxy", client_identifier, offset_ms)
            self._record_seek_attempt(method="proxy", ok=True, target=key, detail="Server proxy seek accepted")
            self._record_seek_success()
            return True
        except Exception as exc:
            logger.warning("Proxy seek failed for %s: %s", client_identifier, exc)
            self._record_seek_attempt(method="proxy", ok=False, target=key, detail=str(exc))
            self._record_seek_failure(
                method="proxy",
                client_identifier=client_identifier,
                detail=str(exc),
            )

        discovered_client, discovery_detail = await self._resolve_companion_client(client_identifier)
        if discovered_client is not None:
            ok, detail = await self._seek_via_companion_client_proxy(discovered_client, offset_ms)
            if ok:
                logger.info("Seeked client %s to %dms via discovered Companion proxy (%s)", client_identifier, offset_ms, discovery_detail)
                self._record_seek_attempt(
                    method="companion_proxy",
                    ok=True,
                    detail=f"{detail}; {discovery_detail}",
                )
                self._record_seek_success()
                return True
            logger.warning("Discovered Companion proxy seek failed for %s: %s (%s)", client_identifier, detail, discovery_detail)
            self._record_seek_attempt(
                method="companion_proxy",
                ok=False,
                detail=f"{detail}; {discovery_detail}",
            )
            self._record_seek_failure(
                method="companion_proxy",
                client_identifier=client_identifier,
                detail=f"{detail}; {discovery_detail}",
            )

            ok, detail = await self._seek_via_companion_client_direct(discovered_client, offset_ms)
            discovered_baseurl = getattr(discovered_client, "_baseurl", "") or ""
            if ok:
                logger.info("Seeked client %s to %dms via discovered Companion direct transport (%s)", client_identifier, offset_ms, discovered_baseurl or discovery_detail)
                self._record_seek_attempt(
                    method="companion_direct",
                    ok=True,
                    target=discovered_baseurl,
                    detail=f"{detail}; {discovery_detail}",
                )
                self._record_seek_success()
                return True
            logger.warning("Discovered Companion direct seek failed for %s: %s (%s)", client_identifier, detail, discovery_detail)
            self._record_seek_attempt(
                method="companion_direct",
                ok=False,
                target=discovered_baseurl,
                detail=f"{detail}; {discovery_detail}",
            )
            self._record_seek_failure(
                method="companion_direct",
                client_identifier=client_identifier,
                detail=f"{detail}; {discovery_detail}",
            )
        else:
            logger.warning("Companion discovery did not resolve client %s: %s", client_identifier, discovery_detail)
            self._record_seek_attempt(
                method="companion_discovery",
                ok=False,
                detail=discovery_detail,
            )
            self._record_seek_failure(
                method="companion_discovery",
                client_identifier=client_identifier,
                detail=discovery_detail,
            )

        legacy_command_id = int(time.time())
        legacy_proxy_url = (
            f"{self.url}/player/playback/seekTo"
            f"?offset={offset_ms}"
            f"&clientIdentifier={client_identifier}"
            f"&commandID={legacy_command_id}"
            f"&X-Plex-Token={self.token}"
        )
        try:
            legacy_resp = await self._http.get(legacy_proxy_url)
            if legacy_resp.status_code < 300:
                logger.info("Seeked client %s to %dms via legacy PMS proxy", client_identifier, offset_ms)
                self._record_seek_attempt(
                    method="proxy_legacy",
                    ok=True,
                    target=legacy_proxy_url,
                    detail="Legacy PMS proxy seek accepted",
                    status_code=legacy_resp.status_code,
                )
                self._record_seek_success()
                return True
            detail = legacy_resp.text[:500] or f"HTTP {legacy_resp.status_code}"
            logger.warning(
                "Legacy proxy seek HTTP %d for %s: %s",
                legacy_resp.status_code,
                client_identifier,
                detail,
            )
            self._record_seek_attempt(
                method="proxy_legacy",
                ok=False,
                target=legacy_proxy_url,
                detail=detail,
                status_code=legacy_resp.status_code,
            )
            self._record_seek_failure(
                method="proxy_legacy",
                client_identifier=client_identifier,
                detail=detail,
                status_code=legacy_resp.status_code,
            )
        except Exception as exc:
            logger.warning("Legacy proxy seek failed for %s: %s", client_identifier, exc)
            self._record_seek_attempt(
                method="proxy_legacy",
                ok=False,
                target=legacy_proxy_url,
                detail=str(exc),
            )
            self._record_seek_failure(
                method="proxy_legacy",
                client_identifier=client_identifier,
                detail=str(exc),
            )

        if not client_address:
            logger.warning("No client_address available for direct seek fallback (client=%s)", client_identifier)
            self._record_seek_attempt(
                method="direct",
                ok=False,
                detail="No client_address available for direct seek fallback",
            )
            self._record_seek_failure(
                method="direct",
                client_identifier=client_identifier,
                detail="No client_address available for direct seek fallback",
            )
            return False

        if self._is_browser_client_title(client_title):
            detail = f"Client {client_title!r} is a web-browser session and does not expose a direct playback-control endpoint"
            logger.warning("%s (client=%s)", detail, client_identifier)
            self._record_seek_attempt(
                method="direct",
                ok=False,
                detail=detail,
                client_address=client_address,
                client_port=client_port,
            )
            self._record_seek_failure(
                method="direct",
                client_identifier=client_identifier,
                detail=detail,
                client_address=client_address,
                client_port=client_port,
            )
            return False

        if self._is_loopback_client_address(client_address):
            detail = f"Client reported loopback address {client_address!r}; direct seek fallback is impossible from the server host"
            logger.warning("%s (client=%s)", detail, client_identifier)
            self._record_seek_attempt(
                method="direct",
                ok=False,
                detail=detail,
                client_address=client_address,
                client_port=client_port,
            )
            self._record_seek_failure(
                method="direct",
                client_identifier=client_identifier,
                detail=detail,
                client_address=client_address,
                client_port=client_port,
            )
            return False

        ports = [client_port, 32500, 3005]
        seen: set[int] = set()
        for port in ports:
            if port in seen:
                continue
            seen.add(port)
            url_host = self._format_client_address_for_url(client_address)
            base = (
                f"http://{url_host}:{port}/player/playback/seekTo"
                f"?offset={offset_ms}"
                f"&type=video"
                f"&commandID={command_id}"
            )
            variants = [
                (
                    f"{base}&X-Plex-Token={self.token}",
                    {"X-Plex-Target-Client-Identifier": client_identifier},
                ),
                (
                    base,
                    {
                        "X-Plex-Token": self.token,
                        "X-Plex-Target-Client-Identifier": client_identifier,
                    },
                ),
                (
                    f"{base}&X-Plex-Token={self.token}",
                    {
                        "X-Plex-Target-Client-Identifier": client_identifier,
                        "X-Plex-Client-Identifier": "leapfrog-server",
                        "X-Plex-Product": "Leapfrog",
                        "X-Plex-Device-Name": "Leapfrog",
                        "X-Plex-Platform": "Windows",
                    },
                ),
                (
                    base,
                    {
                        "X-Plex-Token": self.token,
                        "X-Plex-Target-Client-Identifier": client_identifier,
                        "X-Plex-Client-Identifier": "leapfrog-server",
                        "X-Plex-Product": "Leapfrog",
                        "X-Plex-Device-Name": "Leapfrog",
                        "X-Plex-Platform": "Windows",
                    },
                ),
            ]

            for idx, (url, headers) in enumerate(variants, start=1):
                try:
                    resp = await self._http.get(url, headers=headers)
                    if resp.status_code < 300:
                        logger.info(
                            "Seeked client %s directly at %s:%d to %dms (variant=%d)",
                            client_identifier,
                            client_address,
                            port,
                            offset_ms,
                            idx,
                        )
                        self._record_seek_attempt(
                            method="direct",
                            ok=True,
                            target=url,
                            detail="Direct client seek accepted",
                            client_address=client_address,
                            client_port=port,
                            status_code=resp.status_code,
                            variant=idx,
                        )
                        self._record_seek_success()
                        return True
                    logger.warning(
                        "Direct seek HTTP %d for client %s at %s:%d (variant=%d, body=%s)",
                        resp.status_code,
                        client_identifier,
                        client_address,
                        port,
                        idx,
                        resp.text[:500],
                    )
                    self._record_seek_attempt(
                        method="direct",
                        ok=False,
                        target=url,
                        detail=resp.text[:500] or f"HTTP {resp.status_code}",
                        client_address=client_address,
                        client_port=port,
                        status_code=resp.status_code,
                        variant=idx,
                    )
                    self._record_seek_failure(
                        method="direct",
                        client_identifier=client_identifier,
                        detail=resp.text[:500] or f"HTTP {resp.status_code}",
                        client_address=client_address,
                        client_port=port,
                        status_code=resp.status_code,
                        variant=idx,
                    )
                except Exception as exc:
                    is_connection_failure = isinstance(exc, httpx.ConnectError) or "All connection attempts failed" in str(exc)
                    logger.warning(
                        "Direct seek failed for client %s at %s:%d (variant=%d): %s",
                        client_identifier,
                        client_address,
                        port,
                        idx,
                        exc,
                    )
                    self._record_seek_attempt(
                        method="direct",
                        ok=False,
                        target=url,
                        detail=str(exc),
                        client_address=client_address,
                        client_port=port,
                        variant=idx,
                    )
                    self._record_seek_failure(
                        method="direct",
                        client_identifier=client_identifier,
                        detail=str(exc),
                        client_address=client_address,
                        client_port=port,
                        variant=idx,
                    )
                    if is_connection_failure:
                        # Header/token variants cannot recover a dead TCP endpoint on the same port.
                        break

        return False

    async def probe_seek(
        self,
        session: ActiveSession,
        *,
        offset_ms: int | None = None,
        delta_ms: int = 1000,
    ) -> dict[str, Any]:
        """Run a seek probe against one active session and return diagnostics."""
        target_offset = self.resolve_probe_offset(
            session.position_ms,
            offset_ms=offset_ms,
            delta_ms=delta_ms,
        )
        success = await self.seek(
            session.client_identifier,
            target_offset,
            session.client_address,
            session.client_port,
            session.client_title,
        )
        return {
            "session": {
                "session_key": session.session_key,
                "title": session.full_title,
                "user": session.user,
                "client": session.client_title,
                "client_identifier": session.client_identifier,
                "client_address": session.client_address,
                "client_port": session.client_port,
                "is_controllable": session.is_controllable,
                "position_ms": session.position_ms,
                "rating_key": session.rating_key,
            },
            "probe_success": success,
            "requested_offset_ms": target_offset,
            "last_seek_success_at": self.get_last_seek_success_at(),
            "last_seek_failure": self.get_last_seek_failure(),
            "last_seek_diagnostics": self.get_last_seek_diagnostics(),
        }

    # ── Library ───────────────────────────────────────────────────────────────

    async def get_library_sections(self) -> list[LibrarySection]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            sections = await asyncio.to_thread(srv.library.sections)
            return [
                LibrarySection(
                    section_id=str(s.key),
                    title=s.title,
                    section_type=s.type,
                )
                for s in sections
                if s.type in ("movie", "show")
            ]
        except Exception as exc:
            logger.warning("Failed to fetch library sections: %s", exc)
            return []

    async def get_library_items(self, section_id: str) -> list[MediaItem]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            section = await asyncio.to_thread(srv.library.sectionByID, int(section_id))
            all_items = await asyncio.to_thread(section.all)
        except Exception as exc:
            logger.warning("Failed to fetch library items for section %s: %s", section_id, exc)
            return []

        result = []
        for item in all_items:
            try:
                if item.type == "show":
                    # Enumerate all episodes
                    episodes = await asyncio.to_thread(item.episodes)
                    for ep in episodes:
                        media_item = self._media_item_from_plex(ep, section_id, section.title)
                        if media_item:
                            result.append(media_item)
                else:
                    media_item = self._media_item_from_plex(item, section_id, section.title)
                    if media_item:
                        result.append(media_item)
            except Exception as exc:
                logger.debug("Error parsing library item: %s", exc)

        return result

    def _media_item_from_plex(self, item: Any, library_id: str, library_title: str) -> MediaItem | None:
        try:
            file_path = ""
            if item.media and item.media[0].parts:
                file_path = item.media[0].parts[0].file or ""

            guid = ""
            if hasattr(item, "guids") and item.guids:
                guid = item.guids[0].id
            elif hasattr(item, "guid"):
                guid = item.guid or ""

            title = item.title
            # For episodes, include full show/season/episode context
            if hasattr(item, "grandparentTitle") and item.grandparentTitle:
                title = f"{item.grandparentTitle} – {item.parentTitle} – {item.title}"

            year = getattr(item, "year", None)

            show_guid = getattr(item, "grandparentGuid", "") or ""

            return MediaItem(
                rating_key=str(item.ratingKey),
                plex_guid=guid,
                title=title,
                year=year,
                thumb=item.thumb or "",
                file_path=file_path,
                library_id=library_id,
                library_title=library_title,
                media_type=item.type,
                content_rating=getattr(item, "contentRating", "") or "",
                show_guid=show_guid,
            )
        except Exception:
            return None

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_all_users(self) -> list[PlexUser]:
        try:
            srv = await asyncio.to_thread(self._get_server)
            # Home users / managed users
            users: list[PlexUser] = []
            try:
                home_users = await asyncio.to_thread(srv.myPlexAccount().users)
                for u in home_users:
                    users.append(PlexUser(username=u.username or u.title, thumb=u.thumb or ""))
            except Exception:
                pass
            # Also add the owner
            try:
                account = await asyncio.to_thread(srv.myPlexAccount)
                users.insert(0, PlexUser(username=account.username, thumb=account.thumb or "", is_home_user=False))
            except Exception:
                pass
            return users
        except Exception as exc:
            logger.warning("Failed to fetch users: %s", exc)
            return []

    # ── Thumbnail proxy URL ───────────────────────────────────────────────────

    def thumb_url(self, thumb_path: str) -> str:
        if not thumb_path:
            return ""
        return f"{self.url}{thumb_path}?X-Plex-Token={self.token}"

    async def get_episode_show_art(self, rating_key: str) -> tuple[str, str, str, str, str]:
        """Return (show_guid, show_title, show_thumb_path, show_rating_key, season_rating_key) for an episode rating key.

        Results are cached per rating_key with a TTL of _SHOW_ART_CACHE_TTL_S seconds
        to avoid redundant Plex API calls when listing large TV libraries.
        """
        now = time.monotonic()
        cached = self._show_art_cache.get(rating_key)
        if cached and now - cached[0] < _SHOW_ART_CACHE_TTL_S:
            return cached[1]

        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            show_guid = getattr(item, "grandparentGuid", "") or ""
            show_title = getattr(item, "grandparentTitle", "") or ""
            show_thumb = getattr(item, "grandparentThumb", "") or ""
            show_rating_key = str(getattr(item, "grandparentRatingKey", "") or "")
            season_rating_key = str(getattr(item, "parentRatingKey", "") or "")
            result = (show_guid, show_title, show_thumb, show_rating_key, season_rating_key)
            self._show_art_cache[rating_key] = (now, result)
            return result
        except Exception as exc:
            logger.debug("Failed to resolve show art for rating_key %s: %s", rating_key, exc)
            return "", "", "", "", ""

    async def get_episode_match_info(self, rating_key: str) -> tuple[str, int | None, int | None]:
        """Return (show_title, season_number, episode_number) for one episode rating key."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            show_title = getattr(item, "grandparentTitle", "") or ""
            season_number = getattr(item, "parentIndex", None)
            episode_number = getattr(item, "index", None)
            return (
                str(show_title),
                int(season_number) if season_number is not None else None,
                int(episode_number) if episode_number is not None else None,
            )
        except Exception as exc:
            logger.debug("Failed to resolve episode match info for rating_key %s: %s", rating_key, exc)
            return "", None, None

    async def fetch_image(self, image_path: str) -> tuple[bytes, str]:
        """Fetch an image from Plex and return (bytes, content_type)."""
        if not image_path:
            return b"", ""

        path = image_path if image_path.startswith("/") else f"/{image_path}"
        url = f"{self.url}{path}"
        if "X-Plex-Token=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}X-Plex-Token={self.token}"

        resp = await self._http.get(url)
        if resp.status_code >= 400:
            return b"", ""

        return resp.content, resp.headers.get("content-type", "image/jpeg")

    # ── Leapfrog metadata block ───────────────────────────────────────────────

    def _strip_leapfrog_block(self, summary: str) -> str:
        pattern = r"\n*\[\[LEAPFROG\]\].*?\[\[/LEAPFROG\]\]\n*"
        return re.sub(pattern, "\n", summary or "", flags=re.S).strip()

    def _build_leapfrog_block(self, status: str, segment_count: int, last_scan: str | None = None) -> str:
        stamp = last_scan or datetime.now().strftime("%Y-%m-%d %H:%M")
        return (
            "[[LEAPFROG]]\n"
            "Leapfrog Scan\n"
            f"Status: {status}\n"
            f"Segments: {segment_count}\n"
            f"Last Scan: {stamp}\n"
            "[[/LEAPFROG]]"
        )

    async def update_leapfrog_summary(
        self,
        rating_key: str,
        status: str,
        segment_count: int,
        last_scan: str | None = None,
    ) -> bool:
        """Insert/update a marker-based Leapfrog block in Plex summary metadata."""
        try:
            srv = await asyncio.to_thread(self._get_server)
            item = await asyncio.to_thread(srv.fetchItem, int(rating_key))
            current_summary = getattr(item, "summary", "") or ""

            base_summary = self._strip_leapfrog_block(current_summary)
            leapfrog_block = self._build_leapfrog_block(status, segment_count, last_scan)
            new_summary = f"{base_summary}\n\n{leapfrog_block}".strip() if base_summary else leapfrog_block

            try:
                await asyncio.to_thread(item.editSummary, new_summary)
            except Exception:
                await asyncio.to_thread(item.edit, summary=new_summary)

            logger.info("Updated Plex summary metadata for rating_key=%s", rating_key)
            return True
        except Exception as exc:
            logger.warning("Failed to update Plex summary metadata for rating_key=%s: %s", rating_key, exc)
            return False

    async def close(self) -> None:
        await self._http.aclose()


# Module-level singleton (set by main.py after config loads)
_client: PlexClient | None = None


def get_client() -> PlexClient:
    if _client is None:
        raise RuntimeError("PlexClient not initialised. Call init_client() first.")
    return _client


def init_client(url: str, token: str) -> PlexClient:
    """Create (or replace) the module-level PlexClient singleton.

    The previous AsyncClient is closed before replacement so open connections
    are not leaked on settings changes or reconnects.
    """
    global _client
    if _client is not None:
        # Schedule close on the running event loop without blocking the caller.
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_closed():
                loop.create_task(_client.close())
        except RuntimeError:
            pass  # no running loop — process is tearing down
    _client = PlexClient(url, token)
    return _client
