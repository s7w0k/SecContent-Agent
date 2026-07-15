"""C0/C1 contract and behavior tests for the shared mcp-crawl client."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from clients.mcp_crawl import (
    ERROR_CRAWLER_BUSY,
    ERROR_INVALID_UPSTREAM_RESPONSE,
    ERROR_MCP_UNAVAILABLE,
    ERROR_UNAUTHORIZED,
    ERROR_UPSTREAM_TIMEOUT,
    HEADER_INITIATOR_USER_ID,
    HEADER_REQUEST_ID,
    HEADER_TRACE_ID,
    McpCrawlClient,
    McpCrawlError,
    RequestContext,
)
from pydantic import SecretStr


def _client(handler, **kwargs) -> McpCrawlClient:
    return McpCrawlClient(
        base_url="http://crawler.test:8101",
        api_key=SecretStr("machine-secret"),
        max_retries=kwargs.pop("max_retries", 0),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_crawl_success_contract_and_headers():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer machine-secret"
        assert request.headers[HEADER_REQUEST_ID] == "request-1"
        assert request.headers[HEADER_TRACE_ID] == "trace-1"
        assert request.headers[HEADER_INITIATOR_USER_ID] == "user-1"
        assert request.url.path == "/crawl-news"
        assert request.content == b'{"days":1}'
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "articles": [],
                    "count": 0,
                    "crawled_at": "2026-07-15T10:00:00+08:00",
                    "errors": {},
                    "per_site": {},
                    "per_site_detail": {},
                },
            },
        )

    client = _client(handler)
    result = await client.crawl_news(
        1,
        RequestContext(
            request_id="request-1",
            trace_id="trace-1",
            initiator_user_id="user-1",
        ),
    )

    assert result["ok"] is True
    assert result["data"]["articles"] == []
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_fulltext_contracts():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fetch-fulltext":
            return httpx.Response(200, json={"ok": True, "content_md": "# Body", "length": 6})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {"https://example.com/a": "# A"},
                "success": 1,
                "total": 1,
            },
        )

    async with _client(handler) as client:
        assert await client.fetch_fulltext("https://example.com/a") == "# Body"
        assert await client.fetch_fulltext_batch(["https://example.com/a"]) == {
            "https://example.com/a": "# A"
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "expected_code", "retryable"),
    [
        (401, {"detail": "unauthorized"}, ERROR_UNAUTHORIZED, False),
        (
            429,
            {
                "ok": False,
                "error": {
                    "code": ERROR_CRAWLER_BUSY,
                    "message": "crawler busy",
                    "request_id": "remote-request",
                    "retryable": True,
                },
            },
            ERROR_CRAWLER_BUSY,
            True,
        ),
        (503, {"detail": "down"}, ERROR_MCP_UNAVAILABLE, True),
    ],
)
async def test_http_errors_are_mapped(status, payload, expected_code, retryable):
    client = _client(lambda _request: httpx.Response(status, json=payload))

    with pytest.raises(McpCrawlError) as caught:
        await client.health()

    assert caught.value.code == expected_code
    assert caught.value.retryable is retryable
    assert "machine-secret" not in str(caught.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_timeout_is_retried_then_mapped():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow", request=request)

    client = _client(handler, max_retries=2)
    with (
        patch.object(client, "_backoff", new=AsyncMock()) as backoff,
        pytest.raises(McpCrawlError) as caught,
    ):
        await client.crawl_news(1)

    assert attempts == 3
    assert backoff.await_count == 2
    assert caught.value.code == ERROR_UPSTREAM_TIMEOUT
    await client.aclose()


@pytest.mark.asyncio
async def test_server_error_is_retried_then_mapped():
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, json={"detail": "failed"})

    client = _client(handler, max_retries=1)
    with (
        patch.object(client, "_backoff", new=AsyncMock()),
        pytest.raises(McpCrawlError) as caught,
    ):
        await client.crawl_news(1)

    assert attempts == 2
    assert caught.value.code == ERROR_MCP_UNAVAILABLE
    assert caught.value.retryable is True
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"ok": True, "content_md": 123}),
    ],
)
async def test_invalid_responses_are_rejected(response):
    client = _client(lambda _request: response)

    with pytest.raises(McpCrawlError) as caught:
        await client.fetch_fulltext("https://example.com/a")

    assert caught.value.code == ERROR_INVALID_UPSTREAM_RESPONSE
    await client.aclose()


@pytest.mark.asyncio
async def test_oversized_response_is_rejected():
    client = _client(
        lambda _request: httpx.Response(200, content=b"x" * (1024 * 1024 + 1)),
        max_response_mb=1,
    )

    with pytest.raises(McpCrawlError) as caught:
        await client.health()

    assert caught.value.code == ERROR_INVALID_UPSTREAM_RESPONSE
    await client.aclose()


@pytest.mark.asyncio
async def test_contract_error_redacts_machine_token():
    client = _client(
        lambda _request: httpx.Response(
            401,
            json={
                "ok": False,
                "error": {
                    "code": ERROR_UNAUTHORIZED,
                    "message": "invalid machine-secret",
                    "retryable": False,
                },
            },
        )
    )

    with pytest.raises(McpCrawlError) as caught:
        await client.health()

    assert "machine-secret" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_client_from_settings_uses_same_api_and_worker_configuration():
    from config import Settings

    settings = Settings(
        DEEPSEEK_API_KEY="test",
        MCP_CRAWL_URL="http://remote-crawler:18101/",
        MCP_CRAWL_API_KEY="shared-token",
        MCP_CRAWL_CONNECT_TIMEOUT=4,
        MCP_CRAWL_READ_TIMEOUT=120,
        MCP_CRAWL_MAX_RETRIES=1,
        MCP_CRAWL_MAX_RESPONSE_MB=8,
        MCP_CRAWL_VERIFY_TLS=False,
        _env_file=None,
    )

    client = McpCrawlClient.from_settings(settings)
    assert client.base_url == "http://remote-crawler:18101"
    assert "shared-token" not in repr(settings)
    await client.aclose()


@pytest.mark.asyncio
async def test_client_close_releases_pool():
    client = _client(lambda _request: httpx.Response(200, json={"ok": True}))
    assert client.is_closed is False
    await client.aclose()
    assert client.is_closed is True


@pytest.mark.asyncio
async def test_worker_shutdown_closes_crawl_client_before_database():
    from worker import shutdown

    client = AsyncMock()
    disconnect = AsyncMock()
    with patch("db.mongo.MongoDB.disconnect", new=disconnect):
        await shutdown({"mcp_crawl_client": client})

    client.aclose.assert_awaited_once_with()
    disconnect.assert_awaited_once_with()
