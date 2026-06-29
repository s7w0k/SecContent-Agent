"""
Pytest 全局 fixtures。

使用方式:
    pytest tests/                          # 运行全部
    pytest tests/unit/                     # 仅单元测试
    pytest tests/ -m "not slow"            # 跳过慢测试
    pytest tests/ --cov=services --cov-report=term-missing
"""

import os
import sys
from typing import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

# ── 将 services 子目录加入 Python path ──────────────────────────
_SERVICES_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_svc_paths = [
    os.path.join(_SERVICES_ROOT, "services", "backend"),
    os.path.join(_SERVICES_ROOT, "services", "mcp_crawl"),
    os.path.join(_SERVICES_ROOT, "services", "mcp_wewe"),
]
for _p in _svc_paths:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def test_settings():
    """提供测试用配置（不依赖真实环境变量）。"""
    from backend.config import Settings

    return Settings(
        MONGODB_URI="mongodb://localhost:27017/test_pr_agent",
        MONGODB_DB="test_pr_agent",
        DEEPSEEK_API_KEY="test-deepseek-key",
        MCP_WEWE_URL="http://localhost:8100",
        MCP_CRAWL_URL="http://localhost:8101",
        PIPELINE_SCORE_THRESHOLD=140,
        PIPELINE_CRAWL_DEFAULT_DAYS=1,
    )


@pytest.fixture
def sample_article_data():
    """提供测试用文章样本数据。"""
    return {
        "url_hash": "d41d8cd98f00b204e9800998ecf8427e",
        "title": "Critical MCP Vulnerability Exposes Agent Authentication",
        "url": "https://example.com/article-1",
        "source": "The Hacker News",
        "source_type": "overseas_news",
        "summary": "A critical vulnerability in MCP servers...",
        "summary_cn": "MCP服务器中发现严重漏洞",
        "content_md": "# Critical MCP Vulnerability\n\nFull article text...",
        "is_ai_security": True,
        "is_agent_security": True,
        "category": "MCP协议漏洞",
        "ai_relevance_score": 85,
        "reportability_score": 72,
        "score_reason": "直接涉及Agent身份安全核心领域",
    }


@pytest.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """提供 httpx AsyncClient（用于测试 FastAPI 端点）。"""
    async with AsyncClient(
        base_url="http://test",
        transport=ASGITransport(app=None),  # 子类中替换具体 app
    ) as client:
        yield client
