"""产品路由服务（阶段1 S1-4 / S1-5）。

统一入口 `ProductRoutingService.resolve()`，按 mode 分支：
- `selected`：严格按用户选择的产品（经目录校验），不调用自动匹配；
- `none`：返回空产品列表；
- `auto`：规则匹配（ProductMatcher），可选 LLM 重排，产出
  `ProductRoutingSnapshot`（含路由版本、置信度、歧义标记）。

LLM 重排器（S1-4）：
- 仅向 LLM 传入文章短摘要和最多若干候选产品的 ID/名称/description/命中关键词；
- 严格 JSON schema 输出；
- 返回的产品 ID 必须存在于已发布目录，非法 ID 一律拒绝；
- 任何失败（异常/解析失败/校验失败）回退规则结果，不允许创造目录外产品 ID。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent.product_catalog import ProductCatalogService
from agent.product_matcher import ProductMatch, ProductMatcher
from models.generation_config import (
    ProductRoutingSnapshot,
    ProductTargetMode,
    ResolvedProduct,
    build_routing_snapshot,
)

logger = logging.getLogger("backend.product_routing")

_MAX_CANDIDATES_FOR_LLM = 5
_MAX_SUMMARY_CHARS = 400


class ProductRoutingService:
    """产品路由服务：selected / auto / none 统一解析。"""

    def __init__(
        self,
        catalog: ProductCatalogService | None = None,
        matcher: ProductMatcher | None = None,
        llm_reranker: LLMProductReranker | None = None,
    ):
        self._catalog = catalog or ProductCatalogService()
        self._matcher = matcher or ProductMatcher(self._catalog)
        self._llm = llm_reranker

    async def resolve(
        self,
        article: dict[str, Any],
        mode: ProductTargetMode,
        selected_product_ids: list[str],
        user_id: str,
        *,
        user_products: list[dict[str, Any]] | None = None,
    ) -> ProductRoutingSnapshot:
        """解析文章的路由结果。

        Args:
            article: 文章字典（title/summary_cn/content_md 等）
            mode: selected / auto / none
            selected_product_ids: selected 模式下用户选择的产品 ID
            user_id: 用户 ID（用于隔离用户级产品）
            user_products: 用户级产品列表（可选，仅 auto 模式使用）
        """
        if mode == ProductTargetMode.NONE.value or str(mode).lower() == "none":
            return build_routing_snapshot(mode="none", resolved_products=[])

        if mode == ProductTargetMode.SELECTED.value or str(mode).lower() == "selected":
            return self._resolve_selected(selected_product_ids)

        # auto
        matches = self._matcher.match_by_rules(
            article,
            top_n=_MAX_CANDIDATES_FOR_LLM,
            user_products=user_products,
        )
        if self._llm is not None and matches:
            try:
                matches = await self._llm.rerank(article, matches)
            except Exception as exc:
                logger.warning("[routing] LLM rerank failed, fallback to rules: %s", exc)

        if not matches:
            return build_routing_snapshot(mode="auto", resolved_products=[])

        resolved = [
            ResolvedProduct(
                product_id=m.product_id,
                product_name=m.product_name,
                match_score=m.match_score,
                match_reason=m.match_reason,
                match_source="rule+llm" if self._llm is not None else "rule",
            )
            for m in matches[:2]
        ]
        snapshot = build_routing_snapshot(mode="auto", resolved_products=resolved)
        snapshot.ambiguous = ProductMatcher.is_ambiguous(matches[:2])
        snapshot.confidence = matches[0].match_score if matches else 0
        return snapshot

    def _resolve_selected(self, selected_product_ids: list[str]) -> ProductRoutingSnapshot:
        """selected：严格按用户选择，失败即抛错（不静默回退）。"""
        products = self._catalog.validate_product_ids(
            list(selected_product_ids),
            purpose="draft",
            max_count=5,
        )
        resolved = [
            ResolvedProduct(
                product_id=p.product_id,
                product_name=p.name,
                match_score=100,
                match_reason="用户指定",
                match_source="user_selected",
            )
            for p in products
        ]
        return build_routing_snapshot(mode="selected", resolved_products=resolved)


class LLMProductReranker:
    """可选 LLM 产品重排器（S1-4）。

    使用方式：
        reranker = LLMProductReranker(
            llm_call=lambda prompt: _call_llm(prompt),  # 返回原始文本
        )
        service = ProductRoutingService(llm_reranker=reranker)
        snapshot = await service.resolve(...)
    """

    def __init__(
        self,
        catalog: ProductCatalogService | None = None,
        llm_call: Callable[[str], Awaitable[str]] | None = None,
    ):
        self._catalog = catalog or ProductCatalogService()
        # 未注入 llm_call 时默认禁用（返回原文），保证纯规则路径可离线运行
        self._llm_call = llm_call

    async def rerank(
        self,
        article: dict[str, Any],
        candidates: list[ProductMatch],
    ) -> list[ProductMatch]:
        """对候选排序并校验，返回重排后的 Top 候选（已过滤非法产品 ID）。

        任一环节失败均回退到原规则候选。
        """
        if self._llm_call is None or not candidates:
            return candidates

        summary = self._build_summary(article)
        prompt = self._build_prompt(summary, candidates)
        try:
            raw = await self._llm_call(prompt)
        except Exception as exc:
            logger.warning("[rerank] llm call failed, fallback: %s", exc)
            return candidates

        try:
            ordered_ids = self._parse_and_validate(raw)
        except Exception as exc:
            logger.warning("[rerank] parse/validate failed, fallback: %s", exc)
            return candidates

        by_id = {m.product_id: m for m in candidates}
        ordered = [by_id[pid] for pid in ordered_ids if pid in by_id]
        # 至少保留 1 个结果，否则回退
        return ordered or candidates

    def _build_summary(self, article: dict[str, Any]) -> str:
        title = article.get("title") or ""
        summary = article.get("summary_cn") or article.get("summary") or ""
        return (f"{title}\n{summary}")[:_MAX_SUMMARY_CHARS]

    def _build_prompt(self, summary: str, candidates: list[ProductMatch]) -> str:
        lines = [
            "请根据文章摘要，从候选产品中选择最相关的产品并按相关度排序。",
            '严格只输出 JSON，格式：{"ranked_product_ids": ["product_id", ...]}',
            "",
            f"文章摘要：\n{summary}",
            "",
            "候选产品（product_id | 名称 | 描述 | 命中关键词）：",
        ]
        for m in candidates[:_MAX_CANDIDATES_FOR_LLM]:
            entry = self._catalog.get_product(m.product_id)
            desc = entry.description if entry else ""
            lines.append(f"- {m.product_id} | {m.product_name} | {desc} | {m.match_reason}")
        lines.append("")
        lines.append("仅从以上 product_id 中选择，不得创建新 ID。")
        return "\n".join(lines)

    def _parse_and_validate(self, raw: str) -> list[str]:
        """解析 LLM JSON 并校验所有 ID 均存在于已发布目录。"""
        text = raw.strip()
        # 提取最外层 JSON（模型可能包裹 markdown 代码块）
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("LLM 输出缺少 JSON")
        data = json.loads(text[start : end + 1])
        ids = data.get("ranked_product_ids", [])
        if not isinstance(ids, list):
            raise ValueError("ranked_product_ids 不是列表")

        published = {p.product_id for p in self._catalog.list_products(published_only=True)}
        result: list[str] = []
        seen: set[str] = set()
        for pid in ids:
            pid = str(pid)
            if pid in seen:
                continue
            if pid not in published:
                # 非法/未发布产品 ID 一律拒绝
                logger.warning("[rerank] 拒绝非法产品 ID: %s", pid)
                continue
            seen.add(pid)
            result.append(pid)
        return result


__all__ = ["LLMProductReranker", "ProductMatch", "ProductRoutingService"]
