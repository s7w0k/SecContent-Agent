"""
MCP HTTP Bridge → LangChain Tool 包装

将 mcp-wewe 和 mcp-crawl 的 HTTP API 封装为 LangChain @tool 函数，
供 Agent 流水线按需调用。

工具列表:
  ┌──────────────────────────┬──────────────────────────────────────┐
  │ 工具名                    │ 来源 / 端点                           │
  ├──────────────────────────┼──────────────────────────────────────┤
  │ fetch_wewe_articles      │ mcp-wewe  POST /fetch-yesterday      │
  │ fetch_article_fulltext   │ mcp-wewe  POST /fetch-article        │
  │ analyze_wewe_article     │ mcp-wewe  POST /analyze-article      │
  │ crawl_overseas_news      │ mcp-crawl POST /crawl-news           │
  │ classify_articles        │ mcp-crawl POST /classify             │
  │ query_articles           │ mcp-crawl POST /query                │
  │ get_crawl_stats          │ mcp-crawl GET  /stats                │
  │ export_articles_csv      │ mcp-crawl POST /export-csv           │
  └──────────────────────────┴──────────────────────────────────────┘

使用:
    from agent.tools import create_mcp_toolset
    tools = create_mcp_toolset(wewe_url="http://mcp-wewe:8100",
                                crawl_url="http://mcp-crawl:8101")
    result = await tools["crawl_overseas_news"].ainvoke({"days": 1})
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
from langchain_core.tools import tool

logger = logging.getLogger("backend.agent.tools")

# ═══════════════════════════════════════════════════════════════
# 通用 HTTP 调用辅助
# ═══════════════════════════════════════════════════════════════

DEFAULT_TIMEOUT = 120.0
MAX_RETRIES = 0


async def _http_call(
    method: str,
    url: str,
    json_data: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """通用 HTTP 调用，带超时 + 重试 + 错误处理。

    Args:
        method: HTTP 方法 (GET / POST)
        url: 完整 URL
        json_data: POST 请求体
        timeout: 超时秒数

    Returns:
        {"ok": true, "data": ...} 或 {"ok": false, "error": "..."}
    """
    last_error: str | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method.upper() == "GET":
                    resp = await client.get(url)
                elif method.upper() == "POST":
                    resp = await client.post(url, json=json_data or {})
                else:
                    return {"ok": False, "error": f"Unsupported method: {method}"}

                resp.raise_for_status()
                data = resp.json()

                # 统一返回格式
                if isinstance(data, dict) and "ok" in data:
                    return data
                return {"ok": True, "data": data}

        except httpx.TimeoutException:
            last_error = f"Request timeout after {timeout}s: {url}"
            logger.warning("HTTP timeout (attempt %d/%d): %s", attempt + 1, MAX_RETRIES + 1, url)
        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code}: {url}"
            logger.warning("HTTP error: %s", last_error)
            break  # 非超时错误不重试
        except Exception as e:
            last_error = f"Unexpected error: {e}"
            logger.warning("HTTP call failed: %s", e)
            break

    return {"ok": False, "error": last_error or "Unknown error"}


# ═══════════════════════════════════════════════════════════════
# Tool 工厂 — 为每个端点生成独立 Tool
# ═══════════════════════════════════════════════════════════════


def _make_get_tool(
    name: str,
    description: str,
    base_url: str,
    path: str,
) -> Callable:
    """创建 GET 类 Tool"""

    @tool(name, description=description)
    async def _tool() -> dict:
        url = f"{base_url.rstrip('/')}{path}"
        return await _http_call("GET", url)

    return _tool


def _make_post_tool(
    name: str,
    description: str,
    base_url: str,
    path: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Callable:
    """创建 POST 类 Tool（参数透传）"""

    @tool(name, description=description)
    async def _tool(payload: dict | None = None) -> dict:
        """调用 MCP HTTP Bridge POST 端点。

        Args:
            payload: 端点参数，将作为 JSON body 发送
        """
        if payload is None:
            payload = {}
        url = f"{base_url.rstrip('/')}{path}"
        return await _http_call("POST", url, json_data=payload, timeout=timeout)

    return _tool


# ═══════════════════════════════════════════════════════════════
# Toolset 构建 — 对外唯一入口
# ═══════════════════════════════════════════════════════════════


def create_mcp_toolset(
    wewe_url: str = "http://mcp-wewe:8100",
    crawl_url: str = "http://mcp-crawl:8101",
) -> dict[str, Callable]:
    """创建完整的 MCP Tool 集合。

    Args:
        wewe_url: mcp-wewe HTTP Bridge 地址
        crawl_url: mcp-crawl HTTP Bridge 地址

    Returns:
        {"tool_name": tool_callable, ...}  共 8 个工具
    """
    tools: dict[str, Callable] = {}

    # ── mcp-wewe: 微信公众号 RSS 工具（3 个）──────────────────

    tools["fetch_wewe_articles"] = _make_post_tool(
        name="fetch_wewe_articles",
        description=(
            "获取微信公众号昨日文章列表。"
            "可选参数: rss_url (RSS源地址，默认使用配置中的地址)。"
            "返回: 文章列表，每项含 title/link/summary/source 等字段。"
        ),
        base_url=wewe_url,
        path="/fetch-yesterday",
    )

    tools["fetch_article_fulltext"] = _make_post_tool(
        name="fetch_article_fulltext",
        description=(
            "抓取单篇微信公众号文章的全文 Markdown 内容。"
            "必填参数: link (文章链接，格式 https://mp.weixin.qq.com/s/xxx)。"
            "返回: 文章全文 Markdown 文本。"
        ),
        base_url=wewe_url,
        path="/fetch-article",
    )

    tools["analyze_wewe_article"] = _make_post_tool(
        name="analyze_wewe_article",
        description=(
            "使用 AI 分析单篇公众号文章，生成中文摘要。"
            "必填参数: link (文章链接)。"
            "可选参数: title (文章标题)。"
            "返回: 核心概括、3 个关键信息点、价值判断。"
        ),
        base_url=wewe_url,
        path="/analyze-article",
    )

    # ── mcp-crawl: 海外安全新闻工具（5 个）────────────────────

    tools["crawl_overseas_news"] = _make_post_tool(
        name="crawl_overseas_news",
        description=(
            "爬取海外安全新闻。从 The Hacker News、BleepingComputer、"
            "SecurityWeek 等站点获取最新安全新闻。"
            "可选参数: days (天数，默认 1，最大 30)。"
            "返回: 文章列表，每项含 title/url/source/summary/published_at。"
        ),
        base_url=crawl_url,
        path="/crawl-news",
        timeout=300.0,
    )

    tools["classify_articles"] = _make_post_tool(
        name="classify_articles",
        description=(
            "使用 AI 对文章进行 AI/Agent 安全话题分类。"
            "必填参数: articles_json (JSON 字符串，文章数组)。"
            "可选参数: batch_size (批量大小，默认 25)。"
            "返回: 分类结果，含 is_ai_security/is_agent_security/category/summary_cn。"
        ),
        base_url=crawl_url,
        path="/classify",
    )

    tools["query_articles"] = _make_post_tool(
        name="query_articles",
        description=(
            "查询已爬取的文章数据库。"
            "可选参数: category (分类过滤), days (天数，默认 7), keyword (关键词搜索)。"
            "返回: 匹配的文章列表。"
        ),
        base_url=crawl_url,
        path="/query",
    )

    tools["get_crawl_stats"] = _make_get_tool(
        name="get_crawl_stats",
        description=(
            "获取爬取统计信息：总文章数、来源分布、分类分布、评分分布。返回: 统计数据字典。"
        ),
        base_url=crawl_url,
        path="/stats",
    )

    tools["export_articles_csv"] = _make_post_tool(
        name="export_articles_csv",
        description=(
            "将已分类的 AI 安全文章导出为 CSV 格式。"
            "可选参数: category (按分类筛选，留空则导出全部 AI 安全文章)。"
            "返回: CSV 文本内容。"
        ),
        base_url=crawl_url,
        path="/export-csv",
    )

    logger.info("MCP Toolset created: %d tools", len(tools))
    return tools
