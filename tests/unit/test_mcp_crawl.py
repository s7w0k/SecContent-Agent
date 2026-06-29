"""
mcp-crawl 服务单元测试

运行:
    PYTHONPATH=services/mcp_crawl pytest tests/unit/test_mcp_crawl.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保 mcp_crawl 在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "mcp_crawl"))

from crawler import NewsArticle, NewsCrawler
from classifier import (
    AISecurityClassifier,
    ClassifiedArticle,
    ArticleCategory,
)


# ═══════════════════════════════════════════════════════════
# 1. NewsArticle 数据模型测试
# ═══════════════════════════════════════════════════════════

class TestNewsArticle:
    """NewsArticle 数据模型单元测试"""

    def test_create_article(self):
        art = NewsArticle(
            title="Test Article",
            url="https://example.com/test",
            source="The Hacker News",
            summary="A test summary",
        )
        assert art.title == "Test Article"
        assert art.url == "https://example.com/test"
        assert art.source == "The Hacker News"
        assert art.source_type == "overseas_news"  # default
        assert art.summary == "A test summary"
        assert art.content_md == ""  # default

    def test_url_hash_is_md5(self):
        art = NewsArticle(
            title="T",
            url="https://example.com/unique",
            source="S",
        )
        expected = hashlib.md5(b"https://example.com/unique").hexdigest()
        assert art.url_hash == expected
        assert len(art.url_hash) == 32

    def test_url_hash_consistent(self):
        art1 = NewsArticle(title="A", url="https://a.com", source="S")
        art2 = NewsArticle(title="B", url="https://a.com", source="S")
        assert art1.url_hash == art2.url_hash

    def test_to_dict(self):
        now = datetime(2026, 6, 29, 12, 0, 0)
        art = NewsArticle(
            title="Test",
            url="https://example.com/test",
            source="BleepingComputer",
            published_at=now,
            summary="Summary text",
            content_md="# Markdown content",
        )
        d = art.to_dict()
        assert d["title"] == "Test"
        assert d["url"] == "https://example.com/test"
        assert d["source"] == "BleepingComputer"
        assert d["published_at"] == "2026-06-29"
        assert d["summary"] == "Summary text"
        assert d["content_md"] == "# Markdown content"
        assert len(d["url_hash"]) == 32

    def test_to_dict_no_published_at(self):
        art = NewsArticle(title="T", url="https://x.com", source="S")
        d = art.to_dict()
        assert d["published_at"] == ""

    def test_default_values(self):
        art = NewsArticle(title="T", url="https://x.com", source="S")
        assert art.source_type == "overseas_news"
        assert art.summary == ""
        assert art.content_md == ""
        assert art.published_at is None


# ═══════════════════════════════════════════════════════════
# 2. ClassifiedArticle 数据模型测试
# ═══════════════════════════════════════════════════════════

class TestClassifiedArticle:
    """ClassifiedArticle 数据模型测试"""

    def test_create_default(self):
        ca = ClassifiedArticle(
            title="Test",
            url="https://x.com",
            url_hash="abc123",
            source="S",
        )
        assert ca.title == "Test"
        assert ca.is_ai_security is False
        assert ca.is_agent_security is False
        assert ca.ai_relevance_score == 0
        assert ca.category == ""
        assert ca.classified_at is not None

    def test_create_ai_security(self):
        ca = ClassifiedArticle(
            title="AI Vulnerability Found",
            url="https://x.com",
            url_hash="abc",
            source="S",
            is_ai_security=True,
            is_agent_security=True,
            category="MCP协议漏洞",
            ai_relevance_score=92,
            ai_reason="MCP认证缺陷",
            summary_cn="发现MCP漏洞",
        )
        assert ca.is_ai_security is True
        assert ca.is_agent_security is True
        assert ca.category == "MCP协议漏洞"
        assert ca.ai_relevance_score == 92

    def test_from_article(self):
        art = NewsArticle(
            title="Original",
            url="https://x.com/orig",
            source="The Hacker News",
            published_at=datetime(2026, 6, 29),
            summary="Original summary",
            content_md="# Content",
        )
        ca = ClassifiedArticle.from_article(art)
        assert ca.title == "Original"
        assert ca.url == "https://x.com/orig"
        assert ca.url_hash == art.url_hash
        assert ca.source == "The Hacker News"
        assert ca.published_at == "2026-06-29"
        assert ca.summary == "Original summary"
        assert ca.is_ai_security is False  # default
        assert ca.is_agent_security is False

    def test_to_dict(self):
        ca = ClassifiedArticle(
            title="T", url="https://x.com", url_hash="h", source="S",
            is_ai_security=True, category="提示注入", ai_relevance_score=75,
            summary_cn="摘要", classified_at="2026-06-29 12:00:00",
        )
        d = ca.to_dict()
        assert d["is_ai_security"] is True
        assert d["category"] == "提示注入"
        assert d["ai_relevance_score"] == 75
        assert d["summary_cn"] == "摘要"
        assert d["classified_at"] == "2026-06-29 12:00:00"

    def test_classified_at_auto_generated(self):
        ca = ClassifiedArticle(title="T", url="https://x.com", url_hash="h", source="S")
        assert ca.classified_at is not None
        assert len(ca.classified_at) >= 19  # "YYYY-MM-DD HH:MM:SS"


# ═══════════════════════════════════════════════════════════
# 3. ArticleCategory 枚举测试
# ═══════════════════════════════════════════════════════════

class TestArticleCategory:
    """分类标签枚举测试"""

    def test_all_categories_defined(self):
        categories = list(ArticleCategory)
        assert len(categories) >= 10  # at least 10 categories

    def test_category_values(self):
        assert ArticleCategory.MCP_PROTOCOL.value == "MCP协议漏洞"
        assert ArticleCategory.PROMPT_INJECTION.value == "提示注入"
        assert ArticleCategory.AI_SECURITY.value == "AI安全"
        assert ArticleCategory.AGENT_SECURITY.value == "Agent安全"


# ═══════════════════════════════════════════════════════════
# 4. NewsCrawler 单元测试
# ═══════════════════════════════════════════════════════════

class TestNewsCrawler:
    """NewsCrawler 单元测试（不实际调用 API）"""

    def test_init_requires_api_key(self):
        with pytest.raises(ValueError, match="TAVILY_API_KEY"):
            NewsCrawler(tavily_api_key="")

    def test_init_accepts_api_key(self):
        with patch("tavily.TavilyClient"):
            crawler = NewsCrawler(tavily_api_key="tvly-test-key")
            assert crawler is not None

    def test_sites_configuration(self):
        with patch("tavily.TavilyClient"):
            crawler = NewsCrawler(tavily_api_key="tvly-test")
            assert len(crawler.SITES) == 4
            assert "The Hacker News" in crawler.SITES
            assert crawler.SITES["The Hacker News"]["method"] == "curl_cffi"
            assert crawler.SITES["BleepingComputer"]["method"] == "tavily"

    def test_parse_date_iso(self):
        result = NewsCrawler._parse_date("2026-06-29T12:00:00")
        assert result == datetime(2026, 6, 29, 12, 0, 0)

    def test_parse_date_rfc2822(self):
        result = NewsCrawler._parse_date("Mon, 29 Jun 2026 12:00:00 +0000")
        assert result == datetime(2026, 6, 29, 12, 0, 0)

    def test_parse_date_short(self):
        result = NewsCrawler._parse_date("2026-06-29")
        assert result == datetime(2026, 6, 29)

    def test_parse_date_text(self):
        result = NewsCrawler._parse_date("Jun 29, 2026")
        assert result == datetime(2026, 6, 29)

    def test_parse_date_invalid(self):
        result = NewsCrawler._parse_date("not a date")
        assert result is None

    def test_parse_date_empty(self):
        assert NewsCrawler._parse_date("") is None
        assert NewsCrawler._parse_date(None) is None


# ═══════════════════════════════════════════════════════════
# 5. AISecurityClassifier 单元测试
# ═══════════════════════════════════════════════════════════

class TestAISecurityClassifier:
    """AISecurityClassifier 单元测试"""

    def test_init_requires_api_key(self):
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            AISecurityClassifier(api_key="")

    def test_init_with_api_key(self):
        classifier = AISecurityClassifier(api_key="sk-test-key")
        assert classifier.model == "deepseek-chat"
        assert classifier.client is not None

    def test_init_custom_model(self):
        classifier = AISecurityClassifier(
            api_key="sk-test",
            model="deepseek-reasoner",
            base_url="https://custom.api.com",
        )
        assert classifier.model == "deepseek-reasoner"

    def test_parse_json_code_block(self):
        text = '```json\n[{"index": 0, "is_ai_security": true, "category": "测试"}]\n```'
        result = AISecurityClassifier._parse_json(text)
        assert 0 in result
        assert result[0]["is_ai_security"] is True
        assert result[0]["category"] == "测试"

    def test_parse_json_no_code_block(self):
        text = '[{"index": 0, "is_ai_security": false}]'
        result = AISecurityClassifier._parse_json(text)
        assert 0 in result
        assert result[0]["is_ai_security"] is False

    def test_parse_json_invalid(self):
        result = AISecurityClassifier._parse_json("not json at all")
        assert result == {}

    def test_parse_json_empty(self):
        result = AISecurityClassifier._parse_json("")
        assert result == {}

    def test_classify_prompt_contains_key_terms(self):
        prompt = AISecurityClassifier.CLASSIFY_PROMPT
        assert "AI安全" in prompt
        assert "Agent安全" in prompt
        assert "MCP" in prompt
        assert "Prompt注入" in prompt
        assert "summary_cn" in prompt


# ═══════════════════════════════════════════════════════════
# 6. MCP Server 协议测试
# ═══════════════════════════════════════════════════════════

class TestMCPServerProtocol:
    """MCP Server JSON-RPC 协议测试（不需要 API key）"""

    @pytest.fixture(autouse=True)
    def setup_server(self):
        """导入 server 模块前的环境设置"""
        os.environ["TAVILY_API_KEY"] = "test-tavily-key"
        os.environ["DEEPSEEK_API_KEY"] = "test-deepseek-key"
        import server as srv

        self.server = srv
        yield
        # 清理服务端的全局状态
        self.server._article_cache.clear()

    def test_tools_count(self):
        assert len(self.server.TOOLS) == 5

    def test_tool_names(self):
        names = {t["name"] for t in self.server.TOOLS}
        expected = {"crawl_news", "classify_articles", "query_database", "get_stats", "export_csv"}
        assert names == expected

    def test_all_tools_have_schema(self):
        for tool in self.server.TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert "type" in tool["inputSchema"]

    def test_initialize_response(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = self.server.handle_message(msg)
        assert resp["id"] == 1
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert resp["result"]["serverInfo"]["name"] == "mcp-crawl"
        assert resp["result"]["serverInfo"]["version"] == "1.0.0"

    def test_tools_list_response(self):
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = self.server.handle_message(msg)
        assert resp["id"] == 2
        assert len(resp["result"]["tools"]) == 5

    def test_ping_response(self):
        msg = {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}
        resp = self.server.handle_message(msg)
        assert resp["id"] == 3
        assert resp["result"] == {}

    def test_notification_no_response(self):
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = self.server.handle_message(msg)
        assert resp is None

    def test_unknown_method_error(self):
        msg = {"jsonrpc": "2.0", "id": 4, "method": "unknown_method", "params": {}}
        resp = self.server.handle_message(msg)
        assert resp["id"] == 4
        assert resp["error"]["code"] == -32601
        assert "未知方法" in resp["error"]["message"]

    def test_unknown_tool_error(self):
        msg = {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        }
        resp = self.server.handle_message(msg)
        assert resp["id"] == 5
        assert "未知工具" in resp["result"]["content"][0]["text"]

    def test_tools_call_without_api_key(self):
        """调用 crawl_news 时 API key 缺失应报错"""
        # 临时删除环境变量
        old_key = os.environ.pop("TAVILY_API_KEY", "")
        try:
            msg = {
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "crawl_news", "arguments": {"days": 1}},
            }
            resp = self.server.handle_message(msg)
            text = resp["result"]["content"][0]["text"]
            data = json.loads(text)
            assert data["ok"] is False
            assert "TAVILY_API_KEY" in data["error"]
        finally:
            os.environ["TAVILY_API_KEY"] = old_key

    def test_get_stats_empty_cache(self):
        msg = {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "get_stats", "arguments": {}},
        }
        resp = self.server.handle_message(msg)
        text = resp["result"]["content"][0]["text"]
        data = json.loads(text)
        assert data["ok"] is True
        assert data["data"]["total"] == 0

    def test_query_database_empty(self):
        msg = {
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "query_database", "arguments": {"keyword": "test"}},
        }
        resp = self.server.handle_message(msg)
        text = resp["result"]["content"][0]["text"]
        data = json.loads(text)
        assert data["ok"] is True
        assert data["data"]["count"] == 0

    def test_export_csv_empty(self):
        msg = {
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "export_csv", "arguments": {}},
        }
        resp = self.server.handle_message(msg)
        text = resp["result"]["content"][0]["text"]
        data = json.loads(text)
        assert data["ok"] is True
        assert data["data"]["count"] == 0


# ═══════════════════════════════════════════════════════════
# 7. HTTP Bridge 测试
# ═══════════════════════════════════════════════════════════

class TestHTTPBridge:
    """http_bridge 模块结构测试"""

    def test_app_exists(self):
        import http_bridge
        assert http_bridge.app is not None
        assert http_bridge.app.title == "MCP-Crawl HTTP Bridge"

    def test_app_has_routes(self):
        import http_bridge
        routes = [r.path for r in http_bridge.app.routes]
        assert "/health" in routes
        assert "/tools" in routes
        assert "/call/{tool_name}" in routes
        assert "/crawl-news" in routes
        assert "/classify" in routes
        assert "/query" in routes
        assert "/stats" in routes
        assert "/export-csv" in routes
        assert "/docs" in routes

    def test_mcp_server_path(self):
        import http_bridge
        assert http_bridge.MCP_SERVER_PATH.endswith("server.py")


# ═══════════════════════════════════════════════════════════
# 8. 集成场景测试（用 mock 数据模拟端到端流程）
# ═══════════════════════════════════════════════════════════

class TestIntegrationScenarios:
    """模拟端到端使用场景"""

    @pytest.fixture(autouse=True)
    def setup(self):
        os.environ["TAVILY_API_KEY"] = "test-key"
        os.environ["DEEPSEEK_API_KEY"] = "test-key"
        import server as srv
        self.server = srv
        yield
        self.server._article_cache.clear()

    def _call_tool(self, name: str, args: dict) -> dict:
        msg = {
            "jsonrpc": "2.0", "id": 100, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        resp = self.server.handle_message(msg)
        return json.loads(resp["result"]["content"][0]["text"])

    def test_full_pipeline_mocked(self):
        """模拟：分类 → 查询 → 统计 → 导出 流程（使用注入的缓存数据）"""
        # 步骤 1: 注入模拟分类数据到缓存
        self.server._article_cache = [
            {
                "title": "MCP Protocol Vulnerability Found",
                "url": "https://example.com/mcp-vuln",
                "url_hash": "hash1",
                "source": "The Hacker News",
                "source_type": "overseas_news",
                "published_at": "2026-06-29",
                "summary": "A critical MCP vulnerability...",
                "summary_cn": "MCP协议发现严重漏洞",
                "is_ai_security": True,
                "is_agent_security": True,
                "category": "MCP协议漏洞",
                "ai_relevance_score": 92,
                "ai_reason": "MCP认证缺陷",
                "classified_at": "2026-06-29 12:00:00",
            },
            {
                "title": "Ransomware Attack on Hospital",
                "url": "https://example.com/ransomware",
                "url_hash": "hash2",
                "source": "BleepingComputer",
                "source_type": "overseas_news",
                "published_at": "2026-06-29",
                "summary": "Traditional ransomware...",
                "summary_cn": "",
                "is_ai_security": False,
                "is_agent_security": False,
                "category": "",
                "ai_relevance_score": 0,
                "ai_reason": "传统勒索软件",
                "classified_at": "2026-06-29 12:00:00",
            },
            {
                "title": "New Prompt Injection Technique",
                "url": "https://example.com/prompt-inject",
                "url_hash": "hash3",
                "source": "SecurityWeek",
                "source_type": "overseas_news",
                "published_at": "2026-06-28",
                "summary": "New prompt injection method...",
                "summary_cn": "新型提示注入技术",
                "is_ai_security": True,
                "is_agent_security": False,
                "category": "提示注入",
                "ai_relevance_score": 78,
                "ai_reason": "新型注入攻击",
                "classified_at": "2026-06-29 13:00:00",
            },
        ]

        # 步骤 2: 查询 — 按分类筛选
        result = self._call_tool("query_database", {"category": "MCP协议漏洞"})
        assert result["ok"] is True
        assert result["data"]["count"] == 1
        assert result["data"]["items"][0]["title"] == "MCP Protocol Vulnerability Found"

        # 步骤 3: 查询 — 按关键词
        result = self._call_tool("query_database", {"keyword": "injection"})
        assert result["ok"] is True
        assert result["data"]["count"] == 1
        assert "Prompt Injection" in result["data"]["items"][0]["title"]

        # 步骤 4: 统计
        result = self._call_tool("get_stats", {})
        assert result["ok"] is True
        stats = result["data"]
        assert stats["total"] == 3
        assert stats["ai_security"] == 2
        assert stats["agent_security"] == 1
        assert stats["sources"]["The Hacker News"] == 1
        assert stats["sources"]["BleepingComputer"] == 1
        assert stats["sources"]["SecurityWeek"] == 1
        assert stats["top_categories"]["MCP协议漏洞"] == 1
        assert stats["top_categories"]["提示注入"] == 1

        # 步骤 5: 导出 CSV
        result = self._call_tool("export_csv", {})
        assert result["ok"] is True
        assert result["data"]["count"] == 2  # only AI security articles
        csv = result["data"]["csv"]
        assert "MCP Protocol Vulnerability" in csv
        assert "New Prompt Injection Technique" in csv
        assert "Ransomware Attack" not in csv  # non-AI excluded

        # 步骤 6: 按分类导出 CSV
        result = self._call_tool("export_csv", {"category": "提示注入"})
        assert result["ok"] is True
        assert result["data"]["count"] == 1
        assert "Prompt Injection" in result["data"]["csv"]
