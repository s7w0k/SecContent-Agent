"""LLM 文档重排器（阶段八 11.1）。

按评测决定是否启用（`KNOWLEDGE_LLM_RERANK_ENABLED`）。仅对 BM25 Top-N 候选重排，
不发送全部文档；任一环节失败回退原候选顺序（与产品路由重排一致的安全回退）。

与 `DocumentRetriever` 的同步 `Reranker`（`Callable[[list[str], str], list[str]]`）
兼容：本类提供同步 `rerank` 供同步检索路径使用；同时提供 `rerank_async` 供离线评测
或异步调用方使用。Top-N 截断逻辑抽成纯函数 `top_n_rerank`，便于单测。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("backend.agent.document_reranker")

# 默认只重排前 N 个候选（与 config.KNOWLEDGE_RERANK_TOP_N 对齐）
DEFAULT_LLM_RERANK_TOP_N = 12

# 候选数超过该阈值才值得 LLM 重排（复用 document_retriever 的门槛语义）
DEFAULT_LLM_RERANK_MIN_CANDIDATES = 8

# 发送给 LLM 的每个候选 snippets 预算（字符）
_RERANK_SNIPPET_CHARS = 200


def top_n_rerank(
    ids: list[str],
    reranked_ids: list[str],
    top_n: int,
) -> list[str]:
    """只重排前 top_n 个，其余保持原序。

    - 取 candidates[:top_n] 交给 LLM，得到 reranked_ids（仅保留候选内且有法的）；
    - 重排结果在前，未进入重排窗口的候选按原序补在末尾。
    """
    window = ids[:top_n]
    window_set = set(window)
    valid_reranked = [i for i in reranked_ids if i in window_set]
    # 保序去重
    seen: set[str] = set()
    ordered: list[str] = []
    for i in valid_reranked:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    # 补上重排窗口内未命中的
    for i in window:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    # 重排窗口之后的原序补在末尾
    for i in ids[top_n:]:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered


class LLMDocumentReranker:
    """可选 LLM 文档重排器（阶段八）。

    使用方式：
        reranker = LLMDocumentReranker(
            llm_call=lambda prompt: _call_llm(prompt),  # 返回原始文本（async 或同步）
        )
        ordered = reranker.rerank(doc_ids, query)       # 同步
    或
        ordered = await reranker.rerank_async(doc_ids, query)  # 异步
    """

    def __init__(
        self,
        llm_call: Callable[[str], Any] | None = None,
        top_n: int = DEFAULT_LLM_RERANK_TOP_N,
        min_candidates: int = DEFAULT_LLM_RERANK_MIN_CANDIDATES,
        snippet_chars: int = _RERANK_SNIPPET_CHARS,
        snippet_map: Callable[[str], str] | None = None,
    ):
        self._llm_call = llm_call
        self._top_n = max(1, top_n)
        self._min_candidates = max(1, min_candidates)
        self._snippet_chars = max(1, snippet_chars)
        # 可选：doc_id → 候选内容摘要（标题/摘要），供 LLM 判断相关性。
        # 缺失时仅按 doc_id 重排（LLM 无法语义判断，效果有限）。
        self._snippet_map = snippet_map

    # ── 同步入口（兼容 DocumentRetriever.Reranker） ──────────

    def rerank(self, doc_ids: list[str], query: str) -> list[str]:
        """同步重排；llm_call 为同步函数时直接调用，否则仅在非常见同步失效时回退。"""
        if self._llm_call is None or not doc_ids:
            return list(doc_ids)
        if len(doc_ids) < self._min_candidates:
            return list(doc_ids)
        window = doc_ids[: self._top_n]
        prompt = self._build_prompt(window, query)
        try:
            raw = self._llm_call(prompt)
            if hasattr(raw, "__await__"):
                # 调用方可能误传 async 函数到同步入口：回退原序，避免阻塞
                logger.warning("LLMDocumentReranker.rerank got coroutine; aborting")
                return list(doc_ids)
            ordered = self._parse_and_validate(raw)
        except Exception as exc:
            logger.warning("doc rerank failed, fallback to BM25: %s", exc)
            return list(doc_ids)
        return top_n_rerank(doc_ids, ordered, self._top_n)

    # ── 异步入口（离线评测 / 异步调用方） ────────────────────

    async def rerank_async(self, doc_ids: list[str], query: str) -> list[str]:
        """异步重排；llm_call 为 async 或 sync 均可。"""
        if self._llm_call is None or not doc_ids:
            return list(doc_ids)
        if len(doc_ids) < self._min_candidates:
            return list(doc_ids)
        window = doc_ids[: self._top_n]
        prompt = self._build_prompt(window, query)
        try:
            raw = self._llm_call(prompt)
            if hasattr(raw, "__await__"):
                raw = await raw
            ordered = self._parse_and_validate(raw)
        except Exception as exc:
            logger.warning("doc rerank(async) failed, fallback to BM25: %s", exc)
            return list(doc_ids)
        return top_n_rerank(doc_ids, ordered, self._top_n)

    # ── 内部 ────────────────────────────────────────────────

    def _build_prompt(self, doc_ids: list[str], query: str) -> str:
        lines = [
            "请根据检索查询，对候选文档按相关性从高到低重排。",
            '严格只输出 JSON，格式：{"ranked_doc_ids": ["doc_id", ...]}',
            "",
            f"查询：{query}",
            "",
            "候选文档：",
        ]
        for i in doc_ids:
            if self._snippet_map is not None:
                snippet = self._snippet_map(i)
                if snippet:
                    snippet = snippet[: self._snippet_chars]
                    lines.append(f"- {i}：{snippet}")
                    continue
            lines.append(f"- {i}")
        lines.append("")
        lines.append("仅从以上 ID 中选择，不得引入新 ID。")
        return "\n".join(lines)

    def _parse_and_validate(self, raw: str) -> list[str]:
        """解析 LLM JSON 并校验所有 ID 均不超出输入（防御性）。"""
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("LLM 输出缺少 JSON")
        data = json.loads(text[start : end + 1])
        ids = data.get("ranked_doc_ids", [])
        if not isinstance(ids, list):
            raise ValueError("ranked_doc_ids 不是列表")
        return [str(x) for x in ids]


# 兼容别名：供外部按旧名引用
RerankParserError = ValueError
