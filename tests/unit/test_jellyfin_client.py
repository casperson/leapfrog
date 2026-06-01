"""Unit tests for the Jellyfin client wrapper."""

from __future__ import annotations

import httpx

from leapfrog.jellyfin_client import JellyfinClient


async def test_build_image_url_uses_authenticated_proxy_for_relative_refs():
    client = JellyfinClient("http://jellyfin:8096", "token")
    try:
        assert (
            client.build_image_url("/Items/123/Images/Primary?tag=abc")
            == "/api/server-image?ref=%2FItems%2F123%2FImages%2FPrimary%3Ftag%3Dabc"
        )
    finally:
        await client.close()


async def test_get_library_items_preserves_library_title():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Library/VirtualFolders":
            return httpx.Response(
                200,
                json=[{"ItemId": "lib-1", "Name": "Movies", "CollectionType": "movies"}],
            )
        if request.url.path == "/Items":
            return httpx.Response(
                200,
                json={
                    "Items": [
                        {
                            "Id": "item-1",
                            "Name": "Movie One",
                            "Type": "Movie",
                            "Path": "/media/movie-one.mkv",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = JellyfinClient("http://jellyfin:8096", "token")
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://jellyfin:8096")
    try:
        items = await client.get_library_items("lib-1")
    finally:
        await client.close()

    assert len(items) == 1
    assert items[0].library_title == "Movies"


async def test_get_library_items_uses_cached_library_title_without_refetching_sections():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Library/VirtualFolders":
            return httpx.Response(500, json={"error": "should not be called"})
        if request.url.path == "/Items":
            return httpx.Response(
                200,
                json={
                    "Items": [
                        {
                            "Id": "item-1",
                            "Name": "Movie One",
                            "Type": "Movie",
                            "Path": "/media/movie-one.mkv",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    client = JellyfinClient("http://jellyfin:8096", "token")
    client._library_title_cache["lib-1"] = "Movies"
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://jellyfin:8096")
    try:
        items = await client.get_library_items("lib-1")
    finally:
        await client.close()

    assert len(items) == 1
    assert items[0].library_title == "Movies"
