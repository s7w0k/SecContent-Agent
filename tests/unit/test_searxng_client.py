"""Unit tests for the SearXNG HTTP client.

All network access is mocked via httpx.MockTransport - no real connections.
Run:
    python -m pytest tests/unit/test_searxng_client.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from clients.searxng import (
    SearXNGBadResponseError,
    SearXNGClient,
    SearXNGConnectionError,
    SearXNGForbiddenError,
    SearXNGRateLimitError,
    SearXNGTimeoutError,
)


def _client(handler, **kwargs) -> SearXNGClient:
    """Build a SearXNGClient backed by a MockTransport handler."""
    return SearXNGClient(
        base_url="http://searxng.test:8080",
        max_retries=kwargs.pop("max_retries", 0),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _ok_response(results=None) -> httpx.Response:
    """Return a valid SearXNG JSON response."""
    return httpx.Response(
        200,
        json={
            "results": results or [],
            "unresponsive_engines": [],
            "number_of_results": len(results or []),
        },
        headers={"content-type": "application/json"},
    )


# ── Successful search ──────────────────────────────────


@pytest.mark.asyncio
async def test_search_success_returns_results():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params["q"] == "AI security"
        assert request.url.params["format"] == "json"
        assert request.url.params["language"] == "en"
        return _ok_response(
            [
                {"url": "https://example.com/1", "title": "AI Threat", "content": "..."},
                {"url": "https://example.com/2", "title": "Agent Risk", "content": "..."},
            ]
        )

    client = _client(handler)
    data = await client.search("AI security", language="en")

    assert isinstance(data, dict)
    assert "results" in data
    assert len(data["results"]) == 2
    assert data["results"][0]["url"] == "https://example.com/1"
    await client.aclose()


@pytest.mark.asyncio
async def test_search_passes_categories_and_time_range():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["categories"] = request.url.params.get("categories")
        captured["time_range"] = request.url.params.get("time_range")
        return _ok_response()

    client = _client(handler)
    await client.search("news", categories=["general", "news"], time_range="week")

    assert captured["categories"] == "general,news"
    assert captured["time_range"] == "week"
    await client.aclose()


# ── Connection failure ─────────────────────────────────


@pytest.mark.asyncio
async def test_search_connection_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)
    with pytest.raises(SearXNGConnectionError):
        await client.search("test")
    await client.aclose()


# ── Read timeout ───────────────────────────────────────


@pytest.mark.asyncio
async def test_search_read_timeout():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler)
    with pytest.raises(SearXNGTimeoutError):
        await client.search("test")
    await client.aclose()


# ── 403 Forbidden ──────────────────────────────────────


@pytest.mark.asyncio
async def test_search_403_forbidden():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = _client(handler)
    with pytest.raises(SearXNGForbiddenError):
        await client.search("test")
    await client.aclose()


# ── 429 Rate limit ─────────────────────────────────────


@pytest.mark.asyncio
async def test_search_429_rate_limit():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = _client(handler)
    with pytest.raises(SearXNGRateLimitError):
        await client.search("test")
    await client.aclose()


# ── 503 with retry ─────────────────────────────────────


@pytest.mark.asyncio
async def test_search_503_retried_then_raises_connection_error():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    client = _client(handler, max_retries=2)
    with (
        patch("clients.searxng.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        pytest.raises(SearXNGConnectionError),
    ):
        await client.search("test")

    # 1 initial + 2 retries = 3 attempts
    assert attempts == 3
    assert sleep_mock.await_count == 2
    await client.aclose()


# ── Non-JSON content type ──────────────────────────────


@pytest.mark.asyncio
async def test_search_non_json_content_type():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>", headers={"content-type": "text/html"})

    client = _client(handler)
    with pytest.raises(SearXNGBadResponseError):
        await client.search("test")
    await client.aclose()


# ── Oversized response ─────────────────────────────────


@pytest.mark.asyncio
async def test_search_oversized_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (10 * 1024 * 1024 + 1),
            headers={"content-type": "application/json"},
        )

    client = _client(handler)
    with pytest.raises(SearXNGBadResponseError, match="响应体过大"):
        await client.search("test")
    await client.aclose()


# ── Missing results key ────────────────────────────────


@pytest.mark.asyncio
async def test_search_missing_results_key():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"unresponsive_engines": [], "number_of_results": 0},
            headers={"content-type": "application/json"},
        )

    client = _client(handler)
    with pytest.raises(SearXNGBadResponseError, match="results"):
        await client.search("test")
    await client.aclose()


# ── Non-dict root ──────────────────────────────────────


@pytest.mark.asyncio
async def test_search_non_dict_root():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[1, 2, 3],
            headers={"content-type": "application/json"},
        )

    client = _client(handler)
    with pytest.raises(SearXNGBadResponseError):
        await client.search("test")
    await client.aclose()


# ── 400 bad request ────────────────────────────────────


@pytest.mark.asyncio
async def test_search_400_bad_request():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid query"})

    client = _client(handler)
    with pytest.raises(SearXNGBadResponseError):
        await client.search("test")
    await client.aclose()


# ── Health check ───────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_ok():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        return httpx.Response(200)

    client = _client(handler)
    assert await client.health_check() is True
    await client.aclose()


@pytest.mark.asyncio
async def test_health_check_unavailable():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client(handler)
    assert await client.health_check() is False
    await client.aclose()


@pytest.mark.asyncio
async def test_health_check_swallows_exceptions():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = _client(handler)
    assert await client.health_check() is False
    await client.aclose()


# ── Client close ───────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_closes_underlying_client():
    client = _client(lambda _request: _ok_response())
    assert client._client.is_closed is False
    await client.aclose()
    assert client._client.is_closed is True


# ── from_settings ──────────────────────────────────────


@pytest.mark.asyncio
async def test_from_settings_builds_client():
    from config import Settings

    settings = Settings(
        DEEPSEEK_API_KEY="test",
        SEARXNG_URL="http://searxng.local:8080/",
        SEARXNG_CONNECT_TIMEOUT=2.0,
        SEARXNG_READ_TIMEOUT=10.0,
        SEARXNG_MAX_RETRIES=0,
        _env_file=None,
    )

    client = SearXNGClient.from_settings(settings)
    assert client._base_url == "http://searxng.local:8080"
    assert client._max_retries == 0
    await client.aclose()


# ── Retry on connect error then success ────────────────


@pytest.mark.asyncio
async def test_retry_on_connect_error_then_success():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise httpx.ConnectError("temporary", request=request)
        return _ok_response([{"url": "https://example.com", "title": "ok"}])

    client = _client(handler, max_retries=1)
    with patch("clients.searxng.asyncio.sleep", new=AsyncMock()):
        data = await client.search("test")

    assert attempts == 2
    assert len(data["results"]) == 1
    await client.aclose()


# ── Pool timeout ───────────────────────────────────────


@pytest.mark.asyncio
async def test_search_pool_timeout():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("pool exhausted", request=request)

    client = _client(handler)
    with pytest.raises(SearXNGConnectionError):
        await client.search("test")
    await client.aclose()
