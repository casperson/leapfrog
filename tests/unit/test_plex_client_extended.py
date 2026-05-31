"""Extended unit tests for plex_client.py — mocking asyncio.to_thread paths."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from leapfrog.plex_client import PlexClient, ActiveSession, LibrarySection, MediaItem, PlexUser


def _make_client(seek_transport=None) -> PlexClient:
    c = PlexClient("http://plex:32400", "token")
    if seek_transport:
        c._http = httpx.AsyncClient(transport=seek_transport)
    return c


def _mock_server():
    srv = MagicMock()
    srv.friendlyName = "My Plex"
    return srv


# ── test_connection ────────────────────────────────────────────────────────────

async def test_test_connection_success():
    c = _make_client()
    srv = _mock_server()

    with patch("leapfrog.plex_client.asyncio.to_thread", new=AsyncMock(return_value=srv)):
        ok, name = await c.test_connection()

    assert ok is True
    assert name == "My Plex"


async def test_test_connection_failure():
    c = _make_client()
    with patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("connection refused")):
        ok, msg = await c.test_connection()
    assert ok is False
    assert "connection refused" in msg


# ── get_active_sessions ────────────────────────────────────────────────────────

async def test_get_active_sessions_empty():
    c = _make_client()
    srv = _mock_server()
    srv.sessions = MagicMock(return_value=[])

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv  # _get_server
        return srv.sessions()  # sessions()

    with patch("leapfrog.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sessions = await c.get_active_sessions()

    assert sessions == []


async def test_get_active_sessions_returns_empty_on_exception():
    c = _make_client()
    with patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("network error")):
        sessions = await c.get_active_sessions()
    assert sessions == []


# ── seek ──────────────────────────────────────────────────────────────────────

async def test_seek_success_via_server_proxy():
    c = _make_client()
    srv = _mock_server()
    srv.query = MagicMock(return_value=None)

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        return None  # srv.query

    with patch("leapfrog.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        result = await c.seek("client-id", 30000)

    assert result is True


async def test_seek_succeeds_via_discovered_companion_proxy_after_primary_proxy_failure():
    c = _make_client()

    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(MagicMock(), "baseurl=http://client:32500; product=Plex for iOS; capabilities=playback,timeline"))),
        patch.object(c, "_seek_via_companion_client_proxy", new=AsyncMock(return_value=(True, "Discovered Companion client proxy seek accepted"))),
    ):
        result = await c.seek("client-id", 30000)

    diagnostics = c.get_last_seek_diagnostics()
    assert result is True
    assert diagnostics is not None
    assert diagnostics["attempts"][0]["method"] == "proxy"
    assert diagnostics["attempts"][1]["method"] == "companion_proxy"
    assert diagnostics["attempts"][1]["ok"] is True


async def test_seek_succeeds_via_discovered_companion_direct_after_proxy_failures():
    c = _make_client()
    discovered_client = MagicMock()
    discovered_client._baseurl = "http://client:32500"

    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(discovered_client, "baseurl=http://client:32500; product=Plex for iOS; capabilities=playback,timeline"))),
        patch.object(c, "_seek_via_companion_client_proxy", new=AsyncMock(return_value=(False, "proxy companion failed"))),
        patch.object(c, "_seek_via_companion_client_direct", new=AsyncMock(return_value=(True, "Discovered Companion client direct seek accepted"))),
    ):
        result = await c.seek("client-id", 30000)

    diagnostics = c.get_last_seek_diagnostics()
    assert result is True
    assert diagnostics is not None
    assert diagnostics["attempts"][1]["method"] == "companion_proxy"
    assert diagnostics["attempts"][2]["method"] == "companion_direct"
    assert diagnostics["attempts"][2]["ok"] is True


async def test_seek_falls_back_to_direct_http_on_proxy_failure():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"ok"))
    c = _make_client(seek_transport=transport)

    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(None, "Companion client not found in /clients discovery list"))),
    ):
        result = await c.seek("client-id", 30000, client_address="192.168.1.10", client_port=32500)

    assert result is True


async def test_seek_succeeds_via_legacy_proxy_when_primary_proxy_fails():
    seen_urls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_urls.append(str(req.url))
        return httpx.Response(200, content=b"ok")

    transport = httpx.MockTransport(handler)
    c = _make_client(seek_transport=transport)

    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(None, "Companion client not found in /clients discovery list"))),
    ):
        result = await c.seek("client-id", 30000)

    diagnostics = c.get_last_seek_diagnostics()
    assert result is True
    assert diagnostics is not None
    assert diagnostics["attempts"][0]["method"] == "proxy"
    assert diagnostics["attempts"][1]["method"] == "companion_discovery"
    assert diagnostics["attempts"][2]["method"] == "proxy_legacy"
    assert any("clientIdentifier=client-id" in url for url in seen_urls)
    assert all("type=video" not in url for url in seen_urls if "clientIdentifier=client-id" in url)


async def test_seek_falls_back_to_direct_http_on_proxy_failure_for_ipv6_client():
    seen_hosts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_hosts.append(req.url.host)
        if req.url.host == "plex":
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, content=b"ok")

    transport = httpx.MockTransport(handler)
    c = _make_client(seek_transport=transport)

    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(None, "Companion client not found in /clients discovery list"))),
    ):
        result = await c.seek("client-id", 30000, client_address="2600:1702:8190:4db0::1b", client_port=32500)

    assert result is True
    assert seen_hosts == ["plex", "2600:1702:8190:4db0::1b"]


async def test_seek_records_full_diagnostics_and_redacts_token():
    transport = httpx.MockTransport(lambda req: httpx.Response(401, content=b"unauthorized"))
    c = _make_client(seek_transport=transport)

    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(None, "Companion client not found in /clients discovery list"))),
    ):
        result = await c.seek("client-id", 30000, client_address="192.168.1.10", client_port=32500)

    diagnostics = c.get_last_seek_diagnostics()
    assert result is False
    assert diagnostics is not None
    assert diagnostics["success"] is False
    assert diagnostics["offset_ms"] == 30000
    assert len(diagnostics["attempts"]) >= 3
    assert diagnostics["attempts"][0]["method"] == "proxy"
    assert diagnostics["attempts"][-1]["status_code"] == 401
    targets = [attempt["target"] for attempt in diagnostics["attempts"]]
    assert all("X-Plex-Token=token" not in target for target in targets)
    assert any("<redacted>" in target for target in targets)


async def test_seek_uses_unique_command_ids():
    c = _make_client()
    srv = _mock_server()
    srv.query = MagicMock(return_value=None)
    keys: list[str] = []

    async def fake_to_thread(func, *args, **kwargs):
        if func == c._get_server:
            return srv
        keys.append(args[0])
        return None

    with patch("leapfrog.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        assert await c.seek("client-id", 30000) is True
        assert await c.seek("client-id", 31000) is True

    assert len(keys) == 2
    assert "commandID=" in keys[0]
    assert keys[0] != keys[1]


async def test_seek_returns_false_when_no_client_address_and_proxy_fails():
    c = _make_client()
    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(None, "Companion client not found in /clients discovery list"))),
    ):
        result = await c.seek("client-id", 30000, client_address="")
    assert result is False


async def test_seek_returns_false_without_direct_attempt_for_loopback_client_address():
    transport = httpx.MockTransport(lambda req: httpx.Response(404, content=b"not found"))
    c = _make_client(seek_transport=transport)

    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(None, "Companion client not found in /clients discovery list"))),
    ):
        result = await c.seek("client-id", 30000, client_address="127.0.0.1", client_port=32500)

    diagnostics = c.get_last_seek_diagnostics()
    assert result is False
    assert diagnostics is not None
    assert len(diagnostics["attempts"]) == 4
    assert diagnostics["attempts"][0]["method"] == "proxy"
    assert diagnostics["attempts"][1]["method"] == "companion_discovery"
    assert diagnostics["attempts"][2]["method"] == "proxy_legacy"
    assert diagnostics["attempts"][3]["method"] == "direct"
    assert "loopback address" in diagnostics["attempts"][3]["detail"]


async def test_seek_returns_false_without_direct_attempt_for_browser_client():
    transport = httpx.MockTransport(lambda req: httpx.Response(404, content=b"not found"))
    c = _make_client(seek_transport=transport)

    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(None, "Companion client not found in /clients discovery list"))),
    ):
        result = await c.seek(
            "client-id",
            30000,
            client_address="2600:1702:8190:4db0::1b",
            client_port=32500,
            client_title="Chrome",
        )

    diagnostics = c.get_last_seek_diagnostics()
    assert result is False
    assert diagnostics is not None
    assert len(diagnostics["attempts"]) == 4
    assert diagnostics["attempts"][1]["method"] == "companion_discovery"
    assert diagnostics["attempts"][2]["method"] == "proxy_legacy"
    assert diagnostics["attempts"][3]["method"] == "direct"
    assert "web-browser session" in diagnostics["attempts"][3]["detail"]


async def test_seek_returns_false_when_all_variants_fail():
    transport = httpx.MockTransport(lambda req: httpx.Response(400, content=b"bad"))
    c = _make_client(seek_transport=transport)
    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(None, "Companion client not found in /clients discovery list"))),
    ):
        result = await c.seek("client-id", 30000, client_address="192.168.1.10")
    assert result is False


def test_resolve_probe_offset_prefers_explicit_offset():
    c = _make_client()
    assert c.resolve_probe_offset(5000, offset_ms=9000, delta_ms=1000) == 9000


def test_resolve_probe_offset_uses_positive_delta():
    c = _make_client()
    assert c.resolve_probe_offset(5000, delta_ms=1500) == 6500


async def test_seek_stops_retrying_variants_after_connection_failure_on_same_port():
    c = _make_client()
    with (
        patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("proxy failed")),
        patch.object(c, "_resolve_companion_client", new=AsyncMock(return_value=(None, "Companion client not found in /clients discovery list"))),
    ):
        result = await c.seek("client-id", 30000, client_address="192.168.1.10", client_port=32500)

    diagnostics = c.get_last_seek_diagnostics()
    assert result is False
    assert diagnostics is not None
    direct_attempts = [attempt for attempt in diagnostics["attempts"] if attempt["method"] == "direct"]
    assert len(direct_attempts) == 2
    assert [attempt["client_port"] for attempt in direct_attempts] == [32500, 3005]


# ── get_library_sections ──────────────────────────────────────────────────────

async def test_get_library_sections_returns_list():
    c = _make_client()
    srv = _mock_server()

    mock_section = MagicMock()
    mock_section.key = "1"
    mock_section.title = "Movies"
    mock_section.type = "movie"

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        return [mock_section]  # sections()

    with patch("leapfrog.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sections = await c.get_library_sections()

    assert len(sections) == 1
    assert isinstance(sections[0], LibrarySection)
    assert sections[0].section_id == "1"
    assert sections[0].section_type == "movie"


async def test_get_library_sections_excludes_non_video_sections():
    c = _make_client()
    srv = _mock_server()

    music = MagicMock()
    music.key = "5"
    music.title = "Music"
    music.type = "artist"  # Not movie or show → excluded

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        return [music]

    with patch("leapfrog.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        sections = await c.get_library_sections()

    assert sections == []


async def test_get_library_sections_returns_empty_on_exception():
    c = _make_client()
    with patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("plex down")):
        sections = await c.get_library_sections()
    assert sections == []


# ── _media_item_from_plex ─────────────────────────────────────────────────────

def test_media_item_from_plex_movie():
    c = _make_client()

    mock_item = MagicMock()
    mock_item.type = "movie"
    mock_item.ratingKey = 42
    mock_item.title = "Inception"
    mock_item.year = 2010
    mock_item.thumb = "/thumb/42"
    mock_item.contentRating = "PG-13"
    mock_item.media = [MagicMock()]
    mock_item.media[0].parts = [MagicMock()]
    mock_item.media[0].parts[0].file = "/media/inception.mkv"
    mock_item.guids = [MagicMock()]
    mock_item.guids[0].id = "imdb://tt1375666"
    del mock_item.grandparentTitle

    item = c._media_item_from_plex(mock_item, "lib1", "Movies")
    assert isinstance(item, MediaItem)
    assert item.title == "Inception"
    assert item.file_path == "/media/inception.mkv"
    assert item.rating_key == "42"


def test_media_item_from_plex_episode_includes_show_in_title():
    c = _make_client()

    mock_ep = MagicMock()
    mock_ep.type = "episode"
    mock_ep.ratingKey = 100
    mock_ep.title = "Pilot"
    mock_ep.year = 2020
    mock_ep.thumb = ""
    mock_ep.contentRating = "TV-MA"
    mock_ep.grandparentTitle = "Breaking Bad"
    mock_ep.parentTitle = "Season 1"
    mock_ep.media = [MagicMock()]
    mock_ep.media[0].parts = [MagicMock()]
    mock_ep.media[0].parts[0].file = "/ep.mkv"
    mock_ep.guids = [MagicMock()]
    mock_ep.guids[0].id = "imdb://ep1"

    item = c._media_item_from_plex(mock_ep, "lib2", "Shows")
    assert "Breaking Bad" in item.title
    assert "Pilot" in item.title


def test_media_item_from_plex_returns_none_on_error():
    c = _make_client()
    result = c._media_item_from_plex(None, "lib", "L")
    assert result is None


# ── close ─────────────────────────────────────────────────────────────────────

async def test_close_calls_aclose():
    c = _make_client()
    mock_http = AsyncMock()
    c._http = mock_http
    await c.close()
    mock_http.aclose.assert_awaited_once()


# ── invalidate ────────────────────────────────────────────────────────────────

def test_invalidate_clears_server():
    c = _make_client()
    c._server = MagicMock()
    c.invalidate()
    assert c._server is None


# ── update_leapfrog_summary ──────────────────────────────────────────────────

async def test_update_leapfrog_summary_success():
    c = _make_client()
    srv = _mock_server()

    mock_item = MagicMock()
    mock_item.summary = "Original summary."
    mock_item.editSummary = MagicMock()

    call_no = 0

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return srv
        if "fetchItem" in str(func) or (args and isinstance(args[0], int)):
            return mock_item
        return None

    with patch("leapfrog.plex_client.asyncio.to_thread", side_effect=fake_to_thread):
        result = await c.update_leapfrog_summary("42", "Scanned", 3)

    assert result is True


async def test_update_leapfrog_summary_returns_false_on_exception():
    c = _make_client()
    with patch("leapfrog.plex_client.asyncio.to_thread", side_effect=Exception("fetch failed")):
        result = await c.update_leapfrog_summary("42", "Scanned", 3)
    assert result is False
