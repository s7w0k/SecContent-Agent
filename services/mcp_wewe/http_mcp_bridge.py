"""
HTTP-MCP 桥接脚本 —— 将 wewe_mcp_server.py 包装为 HTTP API，供外部 Agent 调用。

启动:
    python http_mcp_bridge.py
    python http_mcp_bridge.py --port 8080 --host 0.0.0.0

接口:
    GET  /tools                  — 列出所有可用 MCP 工具
    POST /call/<tool_name>       — 调用指定工具（请求体为 JSON 参数）
    POST /check-accounts         — 快捷：检测账号状态
    POST /fetch-yesterday        — 快捷：获取昨日文章
    POST /fetch-article          — 快捷：抓取文章全文
    POST /analyze-article        — 快捷：AI 分析文章
    POST /create-qrcode          — 快捷：创建登录二维码
    POST /poll-login             — 快捷：轮询扫码结果
    POST /save-account           — 快捷：保存账号
    DELETE /account/<id>         — 快捷：删除账号
    GET  /docs                   — Swagger 文档
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
MCP_SERVER_PATH = os.path.join(SCRIPT_DIR, "wewe_mcp_server.py")

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
    # 提取文本内容
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
    title="WeWe RSS MCP HTTP Bridge",
    version="1.0",
    description="将 wewe_mcp_server (stdio MCP) 桥接为 HTTP API",
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
# 通用接口
# ═══════════════════════════════════════════════════════════

@app.get("/tools")
async def list_tools():
    """列出所有可用 MCP 工具及其参数 schema。"""
    return {"tools": _mcp_tools}


@app.post("/call/{tool_name}")
async def call_tool_endpoint(tool_name: str, arguments: dict = Body(default={})):
    """通用工具调用：POST /call/check_accounts  body: {}"""
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

@app.post("/check-accounts")
async def check_accounts():
    """检测 WeWe RSS 所有账号状态。"""
    result = await _call_tool("check_accounts")
    return json.loads(result)


@app.post("/fetch-yesterday")
async def fetch_yesterday(rss_url: str = Body("", embed=True)):
    """获取昨日公众号文章列表。"""
    args = {}
    if rss_url:
        args["rss_url"] = rss_url
    result = await _call_tool("fetch_yesterday_articles", args)
    return json.loads(result)


@app.post("/fetch-article")
async def fetch_article(link: str = Body(..., embed=True)):
    """抓取单篇公众号文章全文。"""
    result = await _call_tool("fetch_article_fulltext", {"link": link})
    return json.loads(result)


@app.post("/analyze-article")
async def analyze_article(link: str = Body(...), title: str = Body("")):
    """AI 分析单篇文章（抓全文 + DeepSeek 摘要）。"""
    result = await _call_tool("analyze_article", {"link": link, "title": title})
    return json.loads(result)


@app.post("/create-qrcode")
async def create_qrcode():
    """创建微信读书登录二维码。"""
    result = await _call_tool("create_login_qrcode")
    return json.loads(result)


@app.post("/poll-login")
async def poll_login(uuid: str = Body(..., embed=True), timeout_seconds: int = Body(120, embed=True)):
    """轮询扫码登录结果。"""
    result = await _call_tool("poll_login_result", {
        "uuid": uuid, "timeout_seconds": timeout_seconds,
    })
    return json.loads(result)


@app.post("/save-account")
async def save_account(vid: str = Body(...), token: str = Body(...), name: str = Body(...)):
    """保存账号到 WeWe RSS。"""
    result = await _call_tool("save_account", {
        "vid": vid, "token": token, "name": name,
    })
    return json.loads(result)


@app.delete("/account/{account_id}")
async def delete_account(account_id: str):
    """删除指定账号。"""
    result = await _call_tool("delete_account", {"account_id": account_id})
    return json.loads(result)


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WeWe RSS MCP HTTP Bridge")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    print(f"[bridge] 启动 HTTP-MCP 桥接服务: http://{args.host}:{args.port}")
    print(f"[bridge] MCP Server: {MCP_SERVER_PATH}")
    uvicorn.run(app, host=args.host, port=args.port)
