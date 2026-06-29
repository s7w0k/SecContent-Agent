#!/usr/bin/env python3
"""
WeWe RSS MCP Server

将 WeWe RSS 工具封装为 MCP (Model Context Protocol) 服务，
供 Claude、Cursor 等智能体按需调用。

MCP 工具列表：
  ┌──────────────────────────┬─────────────────────────────────────┐
  │ 工具名                    │ 功能                                │
  ├──────────────────────────┼─────────────────────────────────────┤
  │ check_accounts           │ 检测所有账号状态                     │
  │ create_login_qrcode      │ 创建登录二维码（返回 base64 图片）    │
  │ poll_login_result        │ 轮询扫码结果                         │
  │ save_account             │ 保存新账号到 WeWe RSS                │
  │ delete_account           │ 删除指定账号                         │
  │ fetch_yesterday_articles │ 获取昨日公众号文章列表                │
  │ fetch_article_fulltext   │ 抓取单篇文章全文                     │
  │ analyze_article          │ AI 分析单篇文章                       │
  └──────────────────────────┴─────────────────────────────────────┘

用法：
  python wewe_mcp_server.py

环境变量（可选，覆盖默认值）：
  WEWE_RSS_URL       WeWe RSS 服务地址
  WEWE_AUTH_CODE     管理后台授权码
  DEEPSEEK_API_KEY   DeepSeek API Key
"""

import sys
import json
import traceback
from typing import Any

# ── Windows GBK → UTF-8 ──
if sys.platform == "win32":
    try:
        sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
        sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", buffering=1)
    except (OSError, AttributeError):
        pass

from wewe_tools.account_tools import (
    check_accounts,
    create_login_qrcode,
    poll_login_result,
    save_account,
    delete_account,
)
from wewe_tools.feed_tools import (
    fetch_yesterday_articles,
    fetch_article_fulltext,
    analyze_articles_with_llm,
)

# ---------------------------------------------------------------------------
#  MCP 协议常量
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "wewe-rss-mcp"
SERVER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
#  工具定义（MCP tools/list schema）
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "check_accounts",
        "description": "检测 WeWe RSS 所有微信读书账号的状态。返回各账号是否正常/失效/禁用/小黑屋，以及是否有可用账号。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "create_login_qrcode",
        "description": "创建微信读书登录二维码。返回二维码图片（base64 PNG）和扫码链接。用户扫码后，使用 poll_login_result 轮询结果。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "poll_login_result",
        "description": "轮询微信读书扫码登录结果。使用 create_login_qrcode 返回的 uuid 查询用户是否已扫码。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uuid": {
                    "type": "string",
                    "description": "create_login_qrcode 返回的 uuid",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "轮询超时秒数，默认 120",
                },
            },
            "required": ["uuid"],
        },
    },
    {
        "name": "save_account",
        "description": "将扫码登录成功后的账号保存到 WeWe RSS（自动启用）。通常在上一步 poll_login_result 成功后调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vid": {"type": "string", "description": "微信读书用户 vid（来自 poll_login_result）"},
                "token": {"type": "string", "description": "微信读书 token（来自 poll_login_result）"},
                "name": {"type": "string", "description": "用户昵称（来自 poll_login_result）"},
            },
            "required": ["vid", "token", "name"],
        },
    },
    {
        "name": "delete_account",
        "description": "从 WeWe RSS 中删除指定账号。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "要删除的账号 ID"},
            },
            "required": ["account_id"],
        },
    },
    {
        "name": "fetch_yesterday_articles",
        "description": "获取昨日通过 WeWe RSS 订阅到的所有公众号文章列表。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rss_url": {
                    "type": "string",
                    "description": "RSS 源地址（可选，默认从配置读取）",
                },
            },
            "required": [],
        },
    },
    {
        "name": "fetch_article_fulltext",
        "description": "抓取单篇微信公众号文章的全文内容（纯文本）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "link": {
                    "type": "string",
                    "description": "文章链接，格式 https://mp.weixin.qq.com/s/xxx",
                },
            },
            "required": ["link"],
        },
    },
    {
        "name": "analyze_article",
        "description": "使用 DeepSeek LLM 对单篇公众号文章进行 AI 摘要分析。返回：核心概括、3 个关键信息点、对技术从业者的价值判断。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "link": {
                    "type": "string",
                    "description": "文章链接",
                },
                "title": {
                    "type": "string",
                    "description": "文章标题（可选，会自动从链接抓取）",
                },
            },
            "required": ["link"],
        },
    },
]

# ---------------------------------------------------------------------------
#  JSON-RPC 消息处理
# ---------------------------------------------------------------------------

def log(msg: str):
    """输出到 stderr（MCP 日志通道）"""
    print(f"[wewe-mcp] {msg}", file=sys.stderr, flush=True)


def send(data: dict):
    """发送 JSON-RPC 响应到 stdout"""
    line = json.dumps(data, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def make_response(req_id: Any, result: Any) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result,
    }


def make_error(req_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


# ---------------------------------------------------------------------------
#  工具调用分发
# ---------------------------------------------------------------------------

def call_tool(name: str, arguments: dict) -> str:
    """调用具体工具，返回 JSON 字符串"""
    log(f"调用工具: {name} 参数: {json.dumps(arguments, ensure_ascii=False)}")

    if name == "check_accounts":
        result = check_accounts()
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "create_login_qrcode":
        result = create_login_qrcode()
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "poll_login_result":
        uuid = arguments.get("uuid", "")
        timeout = arguments.get("timeout_seconds", 120)
        result = poll_login_result(uuid, timeout)
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "save_account":
        result = save_account(
            arguments.get("vid", ""),
            arguments.get("token", ""),
            arguments.get("name", ""),
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "delete_account":
        result = delete_account(arguments.get("account_id", ""))
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "fetch_yesterday_articles":
        rss_url = arguments.get("rss_url", "")
        result = fetch_yesterday_articles(rss_url)
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "fetch_article_fulltext":
        link = arguments.get("link", "")
        result = fetch_article_fulltext(link)
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "analyze_article":
        link = arguments.get("link", "")
        title = arguments.get("title", "")
        # 先抓全文
        fulltext = fetch_article_fulltext(link)
        if not fulltext["ok"]:
            return json.dumps({"ok": False, "error": f"抓取文章失败: {fulltext.get('error')}"}, ensure_ascii=False, indent=2)

        article = {
            "title": title or "未知标题",
            "link": link,
        }
        result = analyze_articles_with_llm([article])
        return json.dumps(result, ensure_ascii=False, indent=2)

    else:
        return json.dumps({"ok": False, "error": f"未知工具: {name}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
#  主循环
# ---------------------------------------------------------------------------

def handle_message(msg: dict) -> dict | None:
    """处理单条 JSON-RPC 消息，返回响应或 None"""
    method = msg.get("method", "")
    params = msg.get("params", {})
    req_id = msg.get("id")

    log(f"收到: {method}")

    # --- 初始化 ---
    if method == "initialize":
        return make_response(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        })

    # --- 工具列表 ---
    if method == "tools/list":
        return make_response(req_id, {"tools": TOOLS})

    # --- 工具调用 ---
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            text = call_tool(tool_name, arguments)
            return make_response(req_id, {
                "content": [
                    {"type": "text", "text": text},
                ],
            })
        except Exception as e:
            log(f"工具调用异常: {traceback.format_exc()}")
            return make_response(req_id, {
                "content": [
                    {"type": "text", "text": json.dumps({
                        "ok": False,
                        "error": str(e),
                    }, ensure_ascii=False)},
                ],
                "isError": True,
            })

    # --- 通知（无需响应） ---
    if method.startswith("notifications/"):
        return None

    # --- 其他（如 ping） ---
    if method == "ping":
        return make_response(req_id, {})

    return make_error(req_id, -32601, f"未知方法: {method}")


def main():
    log(f"MCP Server 启动: {SERVER_NAME} v{SERVER_VERSION}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"JSON 解析失败: {e}")
            continue

        try:
            response = handle_message(msg)
        except Exception:
            log(f"处理消息异常: {traceback.format_exc()}")
            response = make_error(
                msg.get("id"),
                -32603,
                f"内部错误: {traceback.format_exc()}",
            )

        if response is not None:
            send(response)


if __name__ == "__main__":
    main()
