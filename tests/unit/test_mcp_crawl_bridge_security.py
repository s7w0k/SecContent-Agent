"""mcp-crawl HTTP Bridge 的认证、保护与审计测试。"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import http_bridge as bridge
import pytest
from httpx import ASGITransport, AsyncClient

TOKEN = "test-machine-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _secure_bridge(monkeypatch):
    monkeypatch.setenv("MCP_CRAWL_API_KEY", TOKEN)
    monkeypatch.setenv("MCP_CRAWL_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("MCP_CRAWL_MAX_BATCH_URLS", "100")
    bridge.get_bridge_settings.cache_clear()
    old_limiter = bridge._heavy_limiter
    bridge._heavy_limiter = bridge.ConcurrencyLimiter(2)
    yield
    bridge._heavy_limiter = old_limiter
    bridge.get_bridge_settings.cache_clear()


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=bridge.app, raise_app_exceptions=False),
        base_url="http://crawler.test",
    ) as value:
        yield value


@pytest.mark.asyncio
async def test_health_is_public_but_business_endpoint_requires_token(client):
    health = await client.get("/health")
    tools = await client.get("/tools")

    assert health.status_code == 200
    assert tools.status_code == 401
    assert tools.json()["error"]["code"] == "UNAUTHORIZED"
    assert tools.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_wrong_and_correct_machine_token(client):
    wrong = await client.get("/tools", headers={"Authorization": "Bearer wrong"})
    correct = await client.get("/tools", headers=AUTH_HEADERS)

    assert wrong.status_code == 401
    assert correct.status_code == 200
    assert "tools" in correct.json()


@pytest.mark.asyncio
async def test_unauthorized_request_cannot_trigger_crawl(client, monkeypatch):
    call_tool = AsyncMock(return_value='{"ok":true}')
    monkeypatch.setattr(bridge, "_call_tool", call_tool)

    response = await client.post("/crawl-news", json={"days": 1})

    assert response.status_code == 401
    call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_crawl_preserves_success_contract_and_context_log(
    client,
    monkeypatch,
    caplog,
):
    payload = {
        "ok": True,
        "data": {"articles": [], "count": 0, "errors": {}, "per_site": {}},
    }
    monkeypatch.setattr(bridge, "_call_tool", AsyncMock(return_value=json.dumps(payload)))
    headers = {
        **AUTH_HEADERS,
        "X-Request-ID": "request-c3",
        "X-Trace-ID": "trace-c3",
        "X-Initiator-User-ID": "user-c3",
    }

    with caplog.at_level(logging.INFO, logger="mcp-crawl.bridge"):
        response = await client.post("/crawl-news", json={"days": 1}, headers=headers)

    assert response.status_code == 200
    assert response.json() == payload
    assert response.headers["X-Request-ID"] == "request-c3"
    bridge_record = next(record for record in caplog.records if record.name == "mcp-crawl.bridge")
    event = json.loads(bridge_record.message)
    assert event["trace_id"] == "trace-c3"
    assert event["user_id"] == "user-c3"
    assert event["result_count"] == 0
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_invalid_parameters_and_urls_are_rejected(client, monkeypatch):
    call_tool = AsyncMock(return_value='{"ok":true}')
    monkeypatch.setattr(bridge, "_call_tool", call_tool)

    invalid_days = await client.post("/crawl-news", json={"days": 0}, headers=AUTH_HEADERS)
    invalid_url = await client.post(
        "/fetch-fulltext",
        json={"url": "file:///etc/passwd"},
        headers=AUTH_HEADERS,
    )

    assert invalid_days.status_code == 422
    assert invalid_days.json()["error"]["code"] == "INVALID_REQUEST"
    assert invalid_url.status_code == 422
    assert invalid_url.json()["error"]["code"] == "INVALID_URL"
    call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_count_is_limited(client, monkeypatch):
    monkeypatch.setenv("MCP_CRAWL_MAX_BATCH_URLS", "1")
    bridge.get_bridge_settings.cache_clear()

    response = await client.post(
        "/fetch-fulltext-batch",
        json=["https://example.com/1", "https://example.com/2"],
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_BATCH_SIZE"


@pytest.mark.asyncio
async def test_response_size_is_limited(client, monkeypatch):
    monkeypatch.setenv("MCP_CRAWL_MAX_RESPONSE_MB", "1")
    bridge.get_bridge_settings.cache_clear()
    oversized = json.dumps({"ok": True, "data": "x" * (1024 * 1024 + 1)})
    monkeypatch.setattr(bridge, "_call_tool", AsyncMock(return_value=oversized))

    response = await client.post("/crawl-news", json={"days": 1}, headers=AUTH_HEADERS)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_article_count_and_generic_tool_parameters_are_limited(client, monkeypatch):
    monkeypatch.setenv("MCP_CRAWL_MAX_ARTICLES", "1")
    bridge.get_bridge_settings.cache_clear()
    monkeypatch.setitem(bridge._mcp_tools, "crawl_news", {})
    oversized = {
        "ok": True,
        "data": {"articles": [{"id": 1}, {"id": 2}], "count": 2},
    }
    call_tool = AsyncMock(return_value=json.dumps(oversized))
    monkeypatch.setattr(bridge, "_call_tool", call_tool)

    invalid = await client.post(
        "/call/crawl_news",
        json={"days": 31},
        headers=AUTH_HEADERS,
    )
    too_many = await client.post("/crawl-news", json={"days": 1}, headers=AUTH_HEADERS)

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    assert too_many.status_code == 502
    assert too_many.json()["error"]["code"] == "TOO_MANY_ARTICLES"
    call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrency_limit_returns_429(client, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_call(_name, _arguments):
        started.set()
        await release.wait()
        return '{"ok":true,"data":{"count":0}}'

    monkeypatch.setattr(bridge, "_call_tool", slow_call)
    bridge._heavy_limiter = bridge.ConcurrencyLimiter(1)

    first_task = asyncio.create_task(
        client.post("/crawl-news", json={"days": 1}, headers=AUTH_HEADERS)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    second = await client.post("/crawl-news", json={"days": 1}, headers=AUTH_HEADERS)
    release.set()
    first = await first_task

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "1"
    assert second.json()["error"]["code"] == "CRAWLER_BUSY"
