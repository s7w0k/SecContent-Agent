"""产品匹配器 - auto 模式下根据文章内容自动匹配产品。

两段式匹配：
1. 规则召回：根据产品关键词、目录标签和文章实体召回最多 5 个候选产品
2. LLM/评分排序：在候选中选择 Top 1-2，并返回匹配理由

匹配结果必须保存，Worker 后续只使用冻结结果。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agent.product_catalog import ProductCatalogService

logger = logging.getLogger("backend.product_matcher")

# 每个产品的关键词（用于规则召回）
_PRODUCT_KEYWORDS: dict[str, list[str]] = {
    "agent-identity-security": [
        "智能体身份", "agent身份", "身份认证", "授权", "最小权限",
        "agent identity", "权限边界", "身份治理",
    ],
    "agent-security": [
        "智能体安全", "agent安全", "agent防护", "智能体防护",
        "agent runtime", "智能体运行时", "agent检测",
    ],
    "ai-bom": [
        "AI-BOM", "AI物料清单", "AI资产", "供应链安全",
        "AI组件", "模型供应链", "AI bill of materials",
    ],
    "agent-security-gateway": [
        "安全网关", "API网关", "流量管控", "agent网关",
    ],
    "ans": [
        "ANS", "亚信安全网络服务", "网络安全服务",
    ],
}


@dataclass(frozen=True)
class ProductMatch:
    """单个产品匹配结果。"""

    product_id: str
    product_name: str
    match_score: int
    match_reason: str


class ProductMatcher:
    """产品自动匹配器。"""

    def __init__(self, catalog: ProductCatalogService | None = None):
        self._catalog = catalog or ProductCatalogService()

    def match_by_rules(
        self,
        article: dict[str, Any],
        *,
        top_n: int = 2,
        user_products: list[dict[str, Any]] | None = None,
    ) -> list[ProductMatch]:
        """规则召回 + 排序，返回 Top N 匹配产品。

        Args:
            article: 文章字典（title, summary, content, source 等）
            top_n: 返回的最多产品数
            user_products: 用户级产品列表，每项包含
                product_id, name, aliases, keywords

        Returns:
            按匹配分数降序排列的 ProductMatch 列表
        """
        text = self._extract_text(article)
        if not text:
            return []

        text_lower = text.lower()
        scores: list[tuple[str, str, int, list[str]]] = []

        # 全局产品匹配
        published_products = self._catalog.list_products(published_only=True)
        for product in published_products:
            keywords = _PRODUCT_KEYWORDS.get(product.product_id, [])
            hits = [kw for kw in keywords if kw.lower() in text_lower]
            if hits:
                score = len(hits) * 20
                for alias in product.aliases:
                    if alias.lower() in text_lower:
                        score += 10
                scores.append((product.product_id, product.name, min(score, 100), hits))

        # 用户级产品匹配
        if user_products:
            for up in user_products:
                pid = up.get("product_id", "")
                pname = up.get("name", pid)
                aliases = up.get("aliases", [])
                keywords = up.get("keywords", [])
                hits = [kw for kw in keywords if kw.lower() in text_lower]
                for alias in aliases:
                    if alias.lower() in text_lower:
                        hits.append(alias)
                if hits:
                    score = min(len(hits) * 20, 100)
                    scores.append((pid, pname, score, hits[:5]))

        if not scores:
            return []

        # 排序并取 Top N
        scores.sort(key=lambda x: x[2], reverse=True)
        results: list[ProductMatch] = []
        for pid, pname, score, hits in scores[:top_n]:
            reason = f"匹配关键词: {', '.join(hits[:3])}"
            results.append(ProductMatch(
                product_id=pid,
                product_name=pname,
                match_score=score,
                match_reason=reason,
            ))

        return results

    @staticmethod
    def _extract_text(article: dict[str, Any]) -> str:
        """从文章字典中提取文本。"""
        parts = []
        for key in ("title", "summary", "content", "source"):
            value = article.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        return " ".join(parts)

    def to_snapshot(self, matches: list[ProductMatch]) -> list[dict]:
        """将匹配结果转为快照格式。"""
        return [
            {
                "product_id": m.product_id,
                "product_name": m.product_name,
                "match_score": m.match_score,
                "match_reason": m.match_reason,
            }
            for m in matches
        ]
