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
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ═══════════════════════════════════════════════════════════
# 路径 & 编码
# ═══════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER_PATH = os.path.join(SCRIPT_DIR, "server.py")

if sys.platform == "win32":
    try:
        sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
        sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", buffering=1)
    except (OSError, AttributeError):
        pass

# ═══════════════════════════════════════════════════════════
# MCP 会话管理（持久连接，启动时建立，关闭时清理）
# ═══════════════════════════════════════════════════════════

_mcp_session: Optional[ClientSession] = None
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

    print(f"[bridge] MCP 已连接，{len(_mcp_tools)} 个工具可用:")
    for name in _mcp_tools:
        print(f"  - {name}")


async def _shutdown_mcp():
    """关闭 MCP 连接。"""
    global _mcp_session, _stdio_ctx, _session_ctx
    if _session_ctx:
        await _session_ctx.__aexit__(None, None, None)
    if _stdio_ctx:
        await _stdio_ctx.__aexit__(None, None, None)
    _mcp_session = None
    print("[bridge] MCP 连接已关闭")


async def _call_tool(name: str, arguments: dict = None) -> str:
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


@app.post("/classify")
async def classify(articles_json: str = Body(...), batch_size: int = Body(25, embed=True)):
    """AI 分类文章"""
    result = await _call_tool("classify_articles", {
        "articles_json": articles_json,
        "batch_size": batch_size,
    })
    return json.loads(result)


@app.post("/query")
async def query(
    category: str = Body("", embed=True),
    days: int = Body(7, embed=True),
    keyword: str = Body("", embed=True),
):
    """查询已爬取文章"""
    result = await _call_tool("query_database", {
        "category": category,
        "days": days,
        "keyword": keyword,
    })
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

    print(f"[bridge] 启动 HTTP-MCP 桥接服务: http://{args.host}:{args.port}")
    print(f"[bridge] MCP Server: {MCP_SERVER_PATH}")
    uvicorn.run(app, host=args.host, port=args.port)
