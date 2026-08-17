"""阶段化 query 构造（阶段3 S3-2）。

为 ContextBridge 的每个用途构造简短、聚焦的检索 query，用于知识切片与
ContextManager 的 required 文档选择。query 只承载"检索意图"，不注入知识全文。
"""

from __future__ import annotations

from typing import Any

# 各阶段 query 的字符上限（控制 token 与噪声）
_MAX_SCORE_QUERY = 400
_MAX_DRAFT_QUERY = 400
_MAX_REWRITE_QUERY = 400
_MAX_CHAT_QUERY = 400


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def build_score_query(article: dict[str, Any]) -> str:
    """评分阶段 query：聚焦文章标题 + V2 摘要 + 分类，判断产品相关性与事件影响力。"""
    title = article.get("title", "") or ""
    summary = article.get("summary_cn", "") or article.get("summary", "") or ""
    category = article.get("category_v2", "") or article.get("category", "") or ""
    parts = [title, summary]
    if category:
        parts.append(f"分类：{category}")
    return _truncate(" ".join(p for p in parts if p), _MAX_SCORE_QUERY)


def build_draft_query(
    article: dict[str, Any],
    template: Any = None,
    perspective: str | None = None,
) -> str:
    """草稿阶段 query：文章主题 + 模板定位 + 可选视角，指导产品事实与传播角度。"""
    title = article.get("title", "") or ""
    summary = article.get("summary_cn", "") or article.get("summary", "") or ""
    parts = [title, summary]
    tmpl_name = ""
    if template is not None:
        tmpl_name = getattr(template, "name", "") or ""
    if tmpl_name:
        parts.append(f"模板：{tmpl_name}")
    if perspective:
        parts.append(f"视角：{perspective}")
    return _truncate(" ".join(p for p in parts if p), _MAX_DRAFT_QUERY)


def build_rewrite_query(
    article: dict[str, Any],
    draft: dict[str, Any] | None = None,
    issue: str | None = None,
) -> str:
    """重写阶段 query：文章主题 + 上一版草稿问题，聚焦补全与修正。"""
    title = article.get("title", "") or ""
    summary = article.get("summary_cn", "") or article.get("summary", "") or ""
    parts = [title, summary]
    if draft:
        draft_title = draft.get("title", "") or ""
        if draft_title:
            parts.append(f"原稿：{draft_title}")
    if issue:
        parts.append(f"需修正：{issue}")
    return _truncate(" ".join(p for p in parts if p), _MAX_REWRITE_QUERY)


def build_chat_query(
    instruction: str,
    article: dict[str, Any] | None = None,
    draft: dict[str, Any] | None = None,
) -> str:
    """问答/改稿阶段 query：用户指令 + 文章/草稿锚点。"""
    parts = [instruction.strip()]
    if article:
        parts.append(f"文章：{(article.get('title', '') or '')[:80]}")
    if draft:
        parts.append(f"稿：{(draft.get('title', '') or '')[:80]}")
    return _truncate(" ".join(p for p in parts if p), _MAX_CHAT_QUERY)
