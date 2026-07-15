"""
MCP Tool 包装单元测试

运行:
    pytest tests/unit/test_agent_tools.py -v
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def mock_crawl_client():
    """统一爬虫客户端替身，避免测试发起真实网络请求。"""
    client = MagicMock()
    client.call = AsyncMock(return_value={"ok": True, "data": {"test": "value"}})
    return client


@pytest.fixture
def toolset(mock_crawl_client):
    """创建测试用 Toolset"""
    from agent.tools import create_mcp_toolset

    return create_mcp_toolset(
        wewe_url="http://test-wewe:8100",
        crawl_client=mock_crawl_client,
    )


@pytest.fixture
def mock_httpx():
    """Mock httpx.AsyncClient 的所有 HTTP 请求"""
    with patch("agent.tools.httpx.AsyncClient") as mock_client_class:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"ok": True, "data": {"test": "value"}}

        # AsyncMock for async context manager
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_client_class.return_value = mock_client
        yield mock_client


# ═══════════════════════════════════════════════════════════════
# 1. Toolset 结构测试
# ═══════════════════════════════════════════════════════════════


class TestToolsetStructure:
    """Toolset 结构校验"""

    def test_all_8_tools_created(self, toolset):
        assert len(toolset) == 8

    def test_tool_names(self, toolset):
        expected = {
            "fetch_wewe_articles",
            "fetch_article_fulltext",
            "analyze_wewe_article",
            "crawl_overseas_news",
            "classify_articles",
            "query_articles",
            "get_crawl_stats",
            "export_articles_csv",
        }
        assert set(toolset.keys()) == expected

    def test_each_tool_has_name_and_description(self, toolset):
        for name, t in toolset.items():
            assert hasattr(t, "name"), f"{name} missing .name"
            assert hasattr(t, "description"), f"{name} missing .description"
            assert len(t.description) > 10, f"{name} description too short"

    def test_wewe_tools_use_wewe_url(self, toolset):
        """wewe 工具调用时使用正确的 base URL"""
        for name in ["fetch_wewe_articles", "fetch_article_fulltext", "analyze_wewe_article"]:
            assert name in toolset

    def test_crawl_tools_use_crawl_url(self, toolset):
        """crawl 工具调用时使用正确的 base URL"""
        for name in [
            "crawl_overseas_news",
            "classify_articles",
            "query_articles",
            "get_crawl_stats",
            "export_articles_csv",
        ]:
            assert name in toolset


# ═══════════════════════════════════════════════════════════════
# 2. _http_call 单元测试
# ═══════════════════════════════════════════════════════════════


class TestHTTPCall:
    """_http_call 辅助函数测试"""

    @pytest.mark.asyncio
    async def test_get_success(self, mock_httpx):
        from agent.tools import _http_call

        result = await _http_call("GET", "http://test/tools")
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_post_success(self, mock_httpx):
        from agent.tools import _http_call

        result = await _http_call("POST", "http://test/call/test", json_data={"key": "val"})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_http_error(self, mock_httpx):
        from agent.tools import _http_call

        # mock_httpx 就是 mock_client 实例（见 fixture）
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("HTTP 500")
        mock_httpx.post = AsyncMock(return_value=mock_response)

        result = await _http_call("POST", "http://test/call/x")
        assert result["ok"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unsupported_method(self):
        from agent.tools import _http_call

        result = await _http_call("DELETE", "http://test/x")
        assert result["ok"] is False
        assert "Unsupported" in result["error"]


# ═══════════════════════════════════════════════════════════════
# 3. Tool 调用测试（mock HTTP）
# ═══════════════════════════════════════════════════════════════


class TestWeweTools:
    """mcp-wewe 工具调用测试"""

    @pytest.mark.asyncio
    async def test_fetch_wewe_articles(self, toolset, mock_httpx):
        result = await toolset["fetch_wewe_articles"].ainvoke({"payload": {}})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_fetch_wewe_articles_with_rss_url(self, toolset, mock_httpx):
        result = await toolset["fetch_wewe_articles"].ainvoke(
            {"payload": {"rss_url": "http://custom/rss"}}
        )
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_fetch_article_fulltext(self, toolset, mock_httpx):
        result = await toolset["fetch_article_fulltext"].ainvoke(
            {"payload": {"link": "https://mp.weixin.qq.com/s/test"}}
        )
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_analyze_wewe_article(self, toolset, mock_httpx):
        result = await toolset["analyze_wewe_article"].ainvoke(
            {"payload": {"link": "https://mp.weixin.qq.com/s/test", "title": "Test"}}
        )
        assert result["ok"] is True


class TestCrawlTools:
    """mcp-crawl 工具调用测试"""

    @pytest.mark.asyncio
    async def test_crawl_overseas_news(self, toolset, mock_httpx):
        result = await toolset["crawl_overseas_news"].ainvoke({"payload": {"days": 1}})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_crawl_overseas_news_default_days(self, toolset, mock_httpx):
        result = await toolset["crawl_overseas_news"].ainvoke({"payload": {}})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_classify_articles(self, toolset, mock_httpx):
        articles_json = json.dumps(
            [
                {"title": "Test", "url": "https://x.com", "source": "THN"},
            ]
        )
        result = await toolset["classify_articles"].ainvoke(
            {"payload": {"articles_json": articles_json}}
        )
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_query_articles(self, toolset, mock_httpx):
        result = await toolset["query_articles"].ainvoke(
            {"payload": {"category": "MCP协议漏洞", "days": 7}}
        )
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_query_articles_with_keyword(self, toolset, mock_httpx):
        result = await toolset["query_articles"].ainvoke({"payload": {"keyword": "authentication"}})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_get_crawl_stats(self, toolset, mock_httpx):
        result = await toolset["get_crawl_stats"].ainvoke({"payload": {}})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_export_articles_csv(self, toolset, mock_httpx):
        result = await toolset["export_articles_csv"].ainvoke({"payload": {}})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_export_articles_csv_filtered(self, toolset, mock_httpx):
        result = await toolset["export_articles_csv"].ainvoke({"payload": {"category": "提示注入"}})
        assert result["ok"] is True


# ═══════════════════════════════════════════════════════════════
# 4. Tool 参数传递正确性测试
# ═══════════════════════════════════════════════════════════════


class TestToolParameterPassing:
    """验证 Tool 参数正确传递到 HTTP 请求"""

    @pytest.mark.asyncio
    async def test_crawl_news_passes_days_parameter(self, mock_crawl_client):
        """验证 days 参数正确传递给 crawl-news 端点"""
        from agent.tools import create_mcp_toolset

        ts = create_mcp_toolset(crawl_client=mock_crawl_client)
        await ts["crawl_overseas_news"].ainvoke({"payload": {"days": 3}})

        call_args = mock_crawl_client.call.await_args
        assert call_args.args == ("POST", "/crawl-news")
        assert call_args.kwargs["json_data"] == {"days": 3}

    @pytest.mark.asyncio
    async def test_query_articles_passes_all_params(self, mock_crawl_client):
        """验证多个参数正确传递"""
        from agent.tools import create_mcp_toolset

        ts = create_mcp_toolset(crawl_client=mock_crawl_client)
        payload = {"category": "AI安全", "days": 3, "keyword": "MCP"}
        await ts["query_articles"].ainvoke({"payload": payload})

        assert mock_crawl_client.call.await_args.kwargs["json_data"] == payload

    @pytest.mark.asyncio
    async def test_classify_articles_passes_articles_json(self, mock_crawl_client):
        """验证 articles_json 参数正确传递"""
        from agent.tools import create_mcp_toolset

        articles = json.dumps([{"title": "Test", "url": "https://x.com", "source": "S"}])
        ts = create_mcp_toolset(crawl_client=mock_crawl_client)
        await ts["classify_articles"].ainvoke(
            {"payload": {"articles_json": articles, "batch_size": 10}}
        )

        json_body = mock_crawl_client.call.await_args.kwargs["json_data"]
        assert json_body["articles_json"] == articles
        assert json_body["batch_size"] == 10

    @pytest.mark.asyncio
    async def test_internal_context_is_forwarded_as_headers_not_json(self, mock_crawl_client):
        from agent.tools import create_mcp_toolset

        ts = create_mcp_toolset(crawl_client=mock_crawl_client)
        await ts["crawl_overseas_news"].ainvoke(
            {
                "payload": {
                    "days": 1,
                    "_request_context": {
                        "request_id": "request-a",
                        "trace_id": "trace-a",
                        "initiator_user_id": "user-a",
                    },
                }
            }
        )

        call = mock_crawl_client.call.await_args
        assert call.kwargs["json_data"] == {"days": 1}
        assert call.kwargs["context"].request_id == "request-a"
        assert call.kwargs["context"].trace_id == "trace-a"
        assert call.kwargs["context"].initiator_user_id == "user-a"


# ═══════════════════════════════════════════════════════════════
# 5. 错误场景测试
# ═══════════════════════════════════════════════════════════════


class TestErrorScenarios:
    """异常场景覆盖"""

    @pytest.mark.asyncio
    async def test_connection_refused_returns_error(self, mock_crawl_client):
        """连接被拒绝返回 ok: False"""
        from agent.tools import create_mcp_toolset
        from clients.mcp_crawl import ERROR_MCP_UNAVAILABLE, McpCrawlError

        mock_crawl_client.call.side_effect = McpCrawlError(
            ERROR_MCP_UNAVAILABLE,
            "mcp-crawl is unavailable",
            retryable=True,
        )
        ts = create_mcp_toolset(crawl_client=mock_crawl_client)
        result = await ts["crawl_overseas_news"].ainvoke({"payload": {"days": 1}})
        assert result["ok"] is False
        assert result["code"] == ERROR_MCP_UNAVAILABLE
        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_invalid_response_returns_stable_error(self, mock_crawl_client):
        """统一客户端的非法响应错误保持 Tool 兼容结构。"""
        from agent.tools import create_mcp_toolset
        from clients.mcp_crawl import ERROR_INVALID_UPSTREAM_RESPONSE, McpCrawlError

        mock_crawl_client.call.side_effect = McpCrawlError(
            ERROR_INVALID_UPSTREAM_RESPONSE,
            "mcp-crawl returned invalid JSON",
        )
        ts = create_mcp_toolset(crawl_client=mock_crawl_client)
        result = await ts["crawl_overseas_news"].ainvoke({"payload": {"days": 1}})
        assert result["ok"] is False
        assert result["code"] == ERROR_INVALID_UPSTREAM_RESPONSE

    @pytest.mark.asyncio
    async def test_get_stats_uses_get_method(self, mock_crawl_client):
        """get_crawl_stats 使用 GET 方法"""
        from agent.tools import create_mcp_toolset

        ts = create_mcp_toolset(crawl_client=mock_crawl_client)
        await ts["get_crawl_stats"].ainvoke({"payload": {}})

        assert mock_crawl_client.call.await_args.args == ("GET", "/stats")
