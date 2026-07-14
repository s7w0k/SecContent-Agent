"""
HTTP-MCP 桥接脚本 — 将 mcp-crawl MCP Server 包装为 HTTP API，供外部 Agent 调用。

启动:
    python http_bridge.py
    python http_bridge.py --port 8101 --host 0.0.0.0

接口:
    GET  /health                  — 健康检查
    GET  /tools                   — 列出所有可用 MCP 工具
    POST /call/<tool_name>        — 调用指定工具（请求体为 JSON 参数）
    POST /crawl-news              — 快捷：爬取新闻
    POST /classify                — 快捷：AI 分类
    POST /query                   — 快捷：查询文章
    GET  /stats                   — 快捷：获取统计
    POST /export-csv              — 快捷：导出 CSV
    GET  /docs                    — Swagger 文档
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ═══════════════════════════════════════════════════════════
# 路径 & 编码
# ═══════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER_PATH = os.path.join(SCRIPT_DIR, "server.py")

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        with suppress(OSError, AttributeError):
            stream.reconfigure(encoding="utf-8", line_buffering=True)

# ═══════════════════════════════════════════════════════════
# MCP 会话管理（持久连接，启动时建立，关闭时清理）
# ═══════════════════════════════════════════════════════════

_mcp_session: ClientSession | None = None
_mcp_tools: dict[str, dict] = {}
_stdio_ctx = None
_session_ctx = None


async def _init_mcp():
    """启动 MCP Server 子进程并建立会话。"""
    global _mcp_session, _mcp_tools, _stdio_ctx, _session_ctx

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[MCP_SERVER_PATH],
        env={**os.environ},
    )

    _stdio_ctx = stdio_client(server_params)
    read, write = await _stdio_ctx.__aenter__()

    _session_ctx = ClientSession(read, write)
    _mcp_session = await _session_ctx.__aenter__()
    await _mcp_session.initialize()

    # 获取工具列表
    result = await _mcp_session.list_tools()
    for tool in result.tools:
        _mcp_tools[tool.name] = {
            "description": tool.description,
            "inputSchema": tool.inputSchema,
        }

    for _name in _mcp_tools:
        pass


async def _shutdown_mcp():
    """关闭 MCP 连接。"""
    global _mcp_session, _stdio_ctx, _session_ctx
    if _session_ctx:
        await _session_ctx.__aexit__(None, None, None)
    if _stdio_ctx:
        await _stdio_ctx.__aexit__(None, None, None)
    _mcp_session = None


async def _call_tool(name: str, arguments: dict | None = None) -> str:
    """调用 MCP 工具，返回结果文本。"""
    if _mcp_session is None:
        raise HTTPException(status_code=503, detail="MCP 会话未初始化")
    result = await _mcp_session.call_tool(name, arguments or {})
    texts = []
    for item in result.content:
        if hasattr(item, "text"):
            texts.append(item.text)
    return "\n".join(texts)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期：启动时连接 MCP，关闭时清理。"""
    await _init_mcp()
    yield
    await _shutdown_mcp()


# ═══════════════════════════════════════════════════════════
# FastAPI 应用
# ═══════════════════════════════════════════════════════════

app = FastAPI(
    title="MCP-Crawl HTTP Bridge",
    version="1.0",
    description="将 crawler MCP Server (stdio) 桥接为 HTTP API — 海外安全新闻爬虫",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════
# 健康检查
# ═══════════════════════════════════════════════════════════


@app.get("/health")
async def health():
    """服务健康检查"""
    return {
        "ok": True,
        "status": "healthy",
        "mcp_connected": _mcp_session is not None,
        "tools_count": len(_mcp_tools),
    }


# ═══════════════════════════════════════════════════════════
# 通用接口
# ═══════════════════════════════════════════════════════════


@app.get("/tools")
async def list_tools():
    """列出所有可用 MCP 工具及其参数 schema。"""
    return {"tools": _mcp_tools}


@app.post("/call/{tool_name}")
async def call_tool_endpoint(tool_name: str, arguments: dict = Body(default={})):
    """通用工具调用：POST /call/crawl_news  body: {"days": 1}"""
    if tool_name not in _mcp_tools:
        raise HTTPException(status_code=404, detail=f"工具不存在: {tool_name}")
    result_text = await _call_tool(tool_name, arguments)
    try:
        return json.loads(result_text)
    except json.JSONDecodeError:
        return {"result": result_text}


# ═══════════════════════════════════════════════════════════
# 快捷接口（无需记工具名和参数）
# ═══════════════════════════════════════════════════════════


@app.post("/crawl-news")
async def crawl_news(days: int = Body(1, embed=True)):
    """爬取海外安全新闻"""
    result = await _call_tool("crawl_news", {"days": days})
    return json.loads(result)


@app.post("/fetch-fulltext")
async def fetch_fulltext(url: str = Body(..., embed=True)):
    """抓取单篇文章全文（使用 curl_cffi 浏览器指纹模拟）"""
    from crawler import NewsCrawler

    crawler = NewsCrawler()
    content = await crawler.fetch_fulltext(url)
    if not content:
        raise HTTPException(status_code=502, detail="抓取失败：内容为空")
    return {"ok": True, "content_md": content, "length": len(content)}


@app.post("/fetch-fulltext-batch")
async def fetch_fulltext_batch(urls: list[str] = Body(...)):
    """批量异步抓取文章全文（含反风控：域名并发限制+随机延迟+指数退避重试）"""
    from crawler import NewsArticle, NewsCrawler

    crawler = NewsCrawler()
    articles = [NewsArticle(title="", url=u, source="") for u in urls if u]
    results = await crawler.fetch_fulltext_batch(articles)

    # 返回 {url: content_md}
    url_map = {}
    for art in articles:
        if art.url_hash in results:
            url_map[art.url] = results[art.url_hash]
    return {"ok": True, "data": url_map, "success": len(url_map), "total": len(urls)}


@app.post("/classify")
async def classify(articles_json: str = Body(...), batch_size: int = Body(25, embed=True)):
    """AI 分类文章"""
    result = await _call_tool(
        "classify_articles",
        {
            "articles_json": articles_json,
            "batch_size": batch_size,
        },
    )
    return json.loads(result)


@app.post("/query")
async def query(
    category: str = Body("", embed=True),
    days: int = Body(7, embed=True),
    keyword: str = Body("", embed=True),
):
    """查询已爬取文章"""
    result = await _call_tool(
        "query_database",
        {
            "category": category,
            "days": days,
            "keyword": keyword,
        },
    )
    return json.loads(result)


@app.get("/stats")
async def stats():
    """获取爬取统计"""
    result = await _call_tool("get_stats", {})
    return json.loads(result)


@app.post("/export-csv")
async def export_csv(category: str = Body("", embed=True)):
    """导出 AI 安全文章 CSV"""
    result = await _call_tool("export_csv", {"category": category})
    return json.loads(result)


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP-Crawl HTTP Bridge")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)
