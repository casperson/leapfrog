"""Unit tests for the shared media-server client registry."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from leapfrog import media_server


class _StubClient:
    """Minimal client stub for registry lifecycle tests."""

    adapter = "stub"

    def __init__(self) -> None:
        self.close = AsyncMock(return_value=None)


async def test_init_client_closes_previous_client_before_replacing_it():
    first = _StubClient()
    second = _StubClient()
    media_server.clear_client()

    media_server.init_client(first)
    media_server.init_client(second)
    await asyncio.sleep(0)

    first.close.assert_awaited_once()
    assert media_server.get_client() is second
    media_server.clear_client()
    await asyncio.sleep(0)


async def test_clear_client_closes_active_client():
    client = _StubClient()
    media_server.clear_client()

    media_server.init_client(client)
    media_server.clear_client()
    await asyncio.sleep(0)

    client.close.assert_awaited_once()
