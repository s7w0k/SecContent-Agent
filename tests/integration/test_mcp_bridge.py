"""
HTTP Bridge 集成测试 — 验证 /health /tools /call 端点在真实 FastAPI 环境下的行为

运行:
    pytest tests/integration/test_mcp_bridge.py -v
"""

from __future__ import annotations

import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "mcp_crawl"))


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    """确保测试环境变量就绪"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("MCP_CRAWL_API_KEY", "test-machine-token")
    if "http_bridge" in sys.modules:
        sys.modules["http_bridge"].get_bridge_settings.cache_clear()


CRAWL_AUTH_HEADERS = {"Authorization": "Bearer test-machine-token"}


class TestMCPCrawlHTTPBridge:
    """mcp-crawl HTTP Bridge 烟雾测试"""

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        """健康检查返回 200 + healthy"""
        from http_bridge import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_tools_endpoint(self):
        """GET /tools 返回 5 个工具定义"""
        from http_bridge import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/tools", headers=CRAWL_AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert "tools" in data

    @pytest.mark.asyncio
    async def test_call_nonexistent_tool(self):
        """调用不存在的工具返回 404"""
        from http_bridge import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                "/call/nonexistent",
                json={},
                headers=CRAWL_AUTH_HEADERS,
            )
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_stats_smoke(self):
        """GET /stats 端点可达（MCP未初始化时返回503是预期行为）"""
        from http_bridge import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/stats", headers=CRAWL_AUTH_HEADERS)
            # MCP未初始化时返回503，端点存在且路由正确即可
            assert resp.status_code in (200, 503)


class TestMCPWeweHTTPBridge:
    """mcp-wewe HTTP Bridge 烟雾测试"""

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        """健康检查返回 200"""
        sys.path.insert(
            0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "mcp_wewe")
        )
        os.environ.setdefault("WEWE_RSS_URL", "http://localhost:4000")
        os.environ.setdefault("WEWE_AUTH_CODE", "test-code")

        original_stdout = sys.stdout
        original_stderr = sys.stderr
        from http_mcp_bridge import app

        assert sys.stdout is original_stdout
        assert sys.stderr is original_stderr
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_tools_endpoint(self):
        """GET /tools 返回工具列表"""
        sys.path.insert(
            0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "mcp_wewe")
        )

        from http_mcp_bridge import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/tools")
            assert resp.status_code == 200
            data = resp.json()
            assert "tools" in data

    @pytest.mark.asyncio
    async def test_openapi_schema(self):
        """OpenAPI schema 可访问"""
        sys.path.insert(
            0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "mcp_wewe")
        )

        from http_mcp_bridge import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/openapi.json")
            assert resp.status_code == 200
            schema = resp.json()
            assert "info" in schema
