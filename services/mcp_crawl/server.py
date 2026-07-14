#!/usr/bin/env python3
"""
mcp-crawl MCP Server — 海外安全新闻爬虫与分类

将 site_crawl 的爬取和分类能力封装为 MCP (Model Context Protocol) 服务，
供 Claude、Cursor 等智能体按需调用。

MCP 工具列表（5 个）:
  ┌─────────────────────┬──────────────────────────────────┐
  │ 工具名               │ 功能                             │
  ├─────────────────────┼──────────────────────────────────┤
  │ crawl_news          │ 爬取海外安全新闻（支持天数参数）   │
  │ classify_articles   │ AI 分类文章（AI安全/Agent安全）   │
  │ query_database      │ 查询已爬取文章                    │
  │ get_stats           │ 获取爬取统计                      │
  │ export_csv          │ 导出 AI 安全文章 CSV              │
  └─────────────────────┴──────────────────────────────────┘

用法:
    python server.py

环境变量:
    TAVILY_API_KEY     Tavily 搜索 API Key（必需）
    DEEPSEEK_API_KEY   DeepSeek API Key（必需，用于分类）
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import traceback
from collections import Counter
from contextlib import suppress
from typing import Any

# ── Windows GBK → UTF-8 ──
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        with suppress(OSError, AttributeError):
            stream.reconfigure(encoding="utf-8", line_buffering=True)

from classifier import AISecurityClassifier
from crawler import NewsCrawler

# ═══════════════════════════════════════════════════════════
# MCP 协议常量
# ═══════════════════════════════════════════════════════════

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mcp-crawl"
SERVER_VERSION = "1.0.0"

# ═══════════════════════════════════════════════════════════
# 工具定义（MCP tools/list schema）
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {
        "name": "crawl_news",
        "description": "爬取海外安全新闻。从 The Hacker News、BleepingComputer、"
        "SecurityWeek、Help Net Security 等站点爬取最新安全新闻，"
        "返回文章列表（标题、URL、来源、摘要、发布时间）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "爬取最近几天的新闻，默认 1，最大 30",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 30,
                },
            },
            "required": [],
        },
    },
    {
        "name": "classify_articles",
        "description": "使用 DeepSeek LLM 对文章进行 AI/Agent 安全话题分类。"
        "入参为 JSON 序列化的文章数组，返回包含分类标签、相关度评分、中文摘要的结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "articles_json": {
                    "type": "string",
                    "description": "JSON 序列化的文章数组，每项需含 title/url/source/summary",
                },
                "batch_size": {
                    "type": "integer",
                    "description": "每批次分类文章数，默认 25",
                    "default": 25,
                },
            },
            "required": ["articles_json"],
        },
    },
    {
        "name": "query_database",
        "description": "查询已爬取的文章数据库。支持按分类、时间范围、关键词筛选。"
        "注意：此工具依赖外部数据库（MongoDB），当前阶段返回爬取结果的快照。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "分类过滤（如 'MCP协议漏洞'），留空则不筛选",
                },
                "days": {
                    "type": "integer",
                    "description": "查询最近几天的文章，默认 7",
                    "default": 7,
                },
                "keyword": {
                    "type": "string",
                    "description": "标题/摘要关键词搜索，留空则不筛选",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_stats",
        "description": "获取爬取统计信息：总文章数、来源分布、分类分布、评分分布。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "export_csv",
        "description": "将已分类的 AI 安全文章导出为 CSV 格式。可选按分类筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "按分类筛选（如 'MCP协议漏洞'），留空则导出全部 AI 安全文章",
                },
            },
            "required": [],
        },
    },
]

# ═══════════════════════════════════════════════════════════
# JSON-RPC 消息处理
# ═══════════════════════════════════════════════════════════


def log(msg: str):
    """输出到 stderr（MCP 日志通道）"""


def send(data: dict):
    """发送 JSON-RPC 响应到 stdout"""
    line = json.dumps(data, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def make_response(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ═══════════════════════════════════════════════════════════
# 内存中的文章缓存（阶段一：暂代 MongoDB）
# ═══════════════════════════════════════════════════════════

_article_cache: list[dict] = []  # 最近一次爬取 + 分类的结果


# ═══════════════════════════════════════════════════════════
# 工具调用分发
# ═══════════════════════════════════════════════════════════


def call_tool(name: str, arguments: dict) -> str:
    """调用具体工具，返回 JSON 字符串"""
    log(f"调用工具: {name} 参数: {json.dumps(arguments, ensure_ascii=False)}")

    try:
        if name == "crawl_news":
            return _handle_crawl_news(arguments)
        elif name == "classify_articles":
            return _handle_classify(arguments)
        elif name == "query_database":
            return _handle_query(arguments)
        elif name == "get_stats":
            return _handle_stats(arguments)
        elif name == "export_csv":
            return _handle_export(arguments)
        else:
            return json.dumps({"ok": False, "error": f"未知工具: {name}"}, ensure_ascii=False)
    except Exception as e:
        log(f"工具调用异常: {traceback.format_exc()}")
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


# ── crawl_news ──


def _handle_crawl_news(arguments: dict) -> str:
    global _article_cache
    days = int(arguments.get("days", 1))
    days = max(1, min(days, 30))

    crawler = NewsCrawler()

    import asyncio

    loop = asyncio.new_event_loop()
    articles = loop.run_until_complete(crawler.crawl(days=days))
    loop.close()
    _article_cache = [a.to_dict() for a in articles]

    return json.dumps(
        {
            "ok": True,
            "data": {
                "articles": _article_cache,
                "count": len(_article_cache),
                "crawled_at": _now_iso(),
                "errors": getattr(crawler, "_last_errors", {}),
                "per_site": getattr(crawler, "_per_site", {}),
                "per_site_detail": getattr(crawler, "_per_site_detail", {}),
            },
        },
        ensure_ascii=False,
    )


# ── classify_articles ──


def _handle_classify(arguments: dict) -> str:
    global _article_cache
    articles_json = arguments.get("articles_json", "[]")
    batch_size = int(arguments.get("batch_size", 25))

    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not deepseek_key:
        return json.dumps({"ok": False, "error": "DEEPSEEK_API_KEY not set"}, ensure_ascii=False)

    try:
        raw_list = json.loads(articles_json)
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False, "error": f"Invalid JSON: {e}"}, ensure_ascii=False)

    # 转为 NewsArticle 对象
    from crawler import NewsArticle as NA

    articles = [
        NA(
            title=a.get("title", ""),
            url=a.get("url", ""),
            source=a.get("source", ""),
            source_type=a.get("source_type", "overseas_news"),
            summary=a.get("summary", ""),
        )
        for a in raw_list
    ]

    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    classifier = AISecurityClassifier(api_key=deepseek_key)
    classified = loop.run_until_complete(classifier.classify(articles, batch_size=batch_size))
    _article_cache = [c.to_dict() for c in classified]

    return json.dumps(
        {
            "ok": True,
            "data": {
                "classified": _article_cache,
                "count": len(_article_cache),
                "ai_security_count": sum(1 for c in classified if c.is_ai_security),
                "agent_security_count": sum(1 for c in classified if c.is_agent_security),
                "classified_at": _now_iso(),
            },
        },
        ensure_ascii=False,
    )


# ── query_database ──


def _handle_query(arguments: dict) -> str:
    global _article_cache
    category = arguments.get("category", "")
    int(arguments.get("days", 7))
    keyword = (arguments.get("keyword", "") or "").lower()

    results = _article_cache

    if category:
        results = [r for r in results if category in (r.get("category", "") or "")]
    if keyword:
        results = [
            r
            for r in results
            if keyword in (r.get("title", "") or "").lower()
            or keyword in (r.get("summary", "") or "").lower()
            or keyword in (r.get("summary_cn", "") or "").lower()
        ]

    results.sort(key=lambda r: r.get("classified_at", r.get("published_at", "")), reverse=True)

    return json.dumps(
        {
            "ok": True,
            "data": {
                "items": results,
                "count": len(results),
                "total_in_cache": len(_article_cache),
            },
        },
        ensure_ascii=False,
    )


# ── get_stats ──


def _handle_stats(arguments: dict) -> str:
    global _article_cache

    if not _article_cache:
        return json.dumps(
            {"ok": True, "data": {"total": 0, "message": "缓存为空，请先执行 crawl_news"}},
            ensure_ascii=False,
        )

    total = len(_article_cache)
    ai_count = sum(1 for r in _article_cache if r.get("is_ai_security"))
    agent_count = sum(1 for r in _article_cache if r.get("is_agent_security"))

    sources = Counter(r.get("source", "?") for r in _article_cache)
    categories = Counter(
        r.get("category", "未分类") for r in _article_cache if r.get("is_ai_security")
    )

    # 评分分布
    score_dist = {"0-30": 0, "31-60": 0, "61-80": 0, "81-100": 0}
    for r in _article_cache:
        s = r.get("ai_relevance_score", 0)
        if s <= 30:
            score_dist["0-30"] += 1
        elif s <= 60:
            score_dist["31-60"] += 1
        elif s <= 80:
            score_dist["61-80"] += 1
        else:
            score_dist["81-100"] += 1

    return json.dumps(
        {
            "ok": True,
            "data": {
                "total": total,
                "ai_security": ai_count,
                "agent_security": agent_count,
                "sources": dict(sources.most_common()),
                "top_categories": dict(categories.most_common(10)),
                "score_distribution": score_dist,
            },
        },
        ensure_ascii=False,
    )


# ── export_csv ──


def _handle_export(arguments: dict) -> str:
    global _article_cache
    category = arguments.get("category", "")

    records = [r for r in _article_cache if r.get("is_ai_security")]
    if category:
        records = [r for r in records if category in (r.get("category", "") or "")]

    if not records:
        return json.dumps(
            {"ok": True, "data": {"csv": "", "count": 0, "message": "无匹配记录"}},
            ensure_ascii=False,
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["序号", "题目", "来源", "分类", "AI相关度", "Agent安全", "摘要", "链接"])

    for idx, r in enumerate(records, 1):
        category_label = r.get("category", "")
        if r.get("is_agent_security"):
            category_label = f"[Agent] {category_label}"
        writer.writerow(
            [
                idx,
                r.get("title", ""),
                r.get("source", ""),
                category_label,
                r.get("ai_relevance_score", 0),
                "是" if r.get("is_agent_security") else "否",
                r.get("summary_cn", "") or r.get("summary", ""),
                r.get("url", ""),
            ]
        )

    csv_content = buf.getvalue()
    buf.close()

    return json.dumps(
        {
            "ok": True,
            "data": {
                "csv": csv_content,
                "count": len(records),
            },
        },
        ensure_ascii=False,
    )


# ═══════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════


def _now_iso() -> str:
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")


# ═══════════════════════════════════════════════════════════
# 主循环 — JSON-RPC over stdio
# ═══════════════════════════════════════════════════════════


def handle_message(msg: dict) -> dict | None:
    """处理单条 JSON-RPC 消息，返回响应或 None"""
    method = msg.get("method", "")
    params = msg.get("params", {})
    req_id = msg.get("id")

    log(f"收到: {method}")

    # --- 初始化 ---
    if method == "initialize":
        return make_response(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    # --- 工具列表 ---
    if method == "tools/list":
        return make_response(req_id, {"tools": TOOLS})

    # --- 工具调用 ---
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            text = call_tool(tool_name, arguments)
            return make_response(
                req_id,
                {
                    "content": [{"type": "text", "text": text}],
                },
            )
        except Exception as e:
            log(f"工具调用异常: {traceback.format_exc()}")
            return make_response(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "ok": False,
                                    "error": str(e),
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    "isError": True,
                },
            )

    # --- 通知（无需响应） ---
    if method.startswith("notifications/"):
        return None

    # --- ping ---
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
