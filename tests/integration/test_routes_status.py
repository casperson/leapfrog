"""Integration tests for application status routes."""

from __future__ import annotations

from unittest.mock import patch

import pytest


pytestmark = pytest.mark.usefixtures("setup_db")


async def test_get_application_status_returns_expected_shape(http_client):
    with patch("leapfrog.web.routes.status.plex_mod.get_client", side_effect=RuntimeError("not configured")):
        resp = await http_client.get("/api/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["version"]
    assert payload["scanner"]["healthy"] is True
    assert "skipper" in payload
    assert payload["adapters"]["plex"]["runtime_supported"] is True
    assert payload["exports"]["canonical_format"] == "leapfrog.segment.export/v1"
    assert "plex" in payload["exports"]["runtime_adapters"]


async def test_unknown_api_post_returns_404_not_spa_405(http_client):
    resp = await http_client.post("/api/does-not-exist")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
