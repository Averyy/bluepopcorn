"""Integration tests for MCP tools — hits real Seerr API."""

from __future__ import annotations

import json

import pytest

from bluepopcorn.mcp.config import load_config
from bluepopcorn.mcp.server import create_server
from bluepopcorn.seerr import SeerrClient
from mcp.types import CallToolRequest, CallToolRequestParams


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
async def seerr(config):
    client = SeerrClient(
        base_url=config.seerr_url,
        api_key=config.seerr_api_key,
        timeout=config.http_timeout,
    )
    yield client
    await client.close()


@pytest.fixture
def server(config, seerr):
    return create_server(config, seerr)


async def _call(server, name: str, args: dict) -> dict:
    """Call a tool on the server and parse the result."""
    handler = server.request_handlers[CallToolRequest]
    result = await handler(
        CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name=name, arguments=args),
        )
    )
    text = result.root.content[0].text
    if text.startswith("Error:"):
        return {"error": text}
    return json.loads(text)


# ── seerr_search ─────────────────────────────────────────────────────

@pytest.mark.integration
async def test_search(server):
    data = await _call(server, "seerr_search", {"query": "Inception"})
    assert "results" in data
    assert len(data["results"]) > 0
    item = data["results"][0]
    assert "tmdb_id" in item
    assert "title" in item
    assert "media_type" in item
    assert "status" in item


@pytest.mark.integration
async def test_search_empty(server):
    data = await _call(server, "seerr_search", {"query": "xyzzyplugh999"})
    assert "results" in data
    assert len(data["results"]) == 0


# ── seerr_details ────────────────────────────────────────────────────

@pytest.mark.integration
async def test_details(server):
    # Inception = movie tmdb_id 27205
    data = await _call(server, "seerr_details", {"tmdb_id": 27205, "media_type": "movie"})
    assert data["tmdb_id"] == 27205
    assert "title" in data
    assert "overview" in data
    assert "status" in data
    assert "media_type" in data


@pytest.mark.integration
async def test_details_not_found(server):
    data = await _call(server, "seerr_details", {"tmdb_id": 999999999, "media_type": "movie"})
    assert "error" in data


# ── seerr_request (dedup only) ───────────────────────────────────────

@pytest.mark.integration
async def test_request_dedup(server):
    # Inception (27205) should be available — verify dedup returns already_exists
    data = await _call(server, "seerr_request", {"tmdb_id": 27205, "media_type": "movie"})
    assert data.get("already_exists") is True
    assert "title" in data
    assert "status" in data


# ── seerr_recommend ──────────────────────────────────────────────────

@pytest.mark.integration
async def test_recommend_trending(server):
    data = await _call(server, "seerr_recommend", {"trending": True})
    assert "results" in data
    assert len(data["results"]) > 0


@pytest.mark.integration
async def test_recommend_genre(server):
    data = await _call(server, "seerr_recommend", {"genre": "sci-fi"})
    assert "results" in data
    assert len(data["results"]) > 0


@pytest.mark.integration
async def test_recommend_similar(server):
    data = await _call(server, "seerr_recommend", {"similar_to": "Inception"})
    assert "results" in data
    assert len(data["results"]) > 0
    assert "similar_to" in data


@pytest.mark.integration
async def test_recommend_empty_args(server):
    data = await _call(server, "seerr_recommend", {})
    assert "error" in data


# ── seerr_recent ─────────────────────────────────────────────────────

@pytest.mark.integration
async def test_recent(server):
    data = await _call(server, "seerr_recent", {})
    assert "available" in data
    assert "requested" in data
    assert "page" in data
