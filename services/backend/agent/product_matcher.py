"""产品匹配器 - auto 模式下根据文章内容自动匹配产品。

两段式匹配：
1. 规则召回：根据产品关键词、目录标签和文章实体召回最多 N 个候选产品
2. LLM/评分排序（可选，见 product_routing.py）：在候选中排序 Top 1-2

阶段1 改进（S1-1 / S1-3）：
- `_field_texts` 按优先级读取 `title/summary_cn/summary/content_md/content/category_v2/tags/source`，
  并设置最大文本长度，避免将超长全文直接交给路由器。
- 匹配前做空白归一化（去掉空格并小写），解决 "AI 资产" vs "AI资产"、"agent runtime" vs "agentruntime" 的召回断链。
- 分字段加权：标题命中 > 摘要 > 正文；产品名/别名命中权重更高。
- 通用弱词（如"供应链安全"）即便命中也保持低权重，避免单独形成高置信。
- 提供置信度与歧义判定（`CONFIDENCE_HIGH` / `is_ambiguous`）。

匹配结果必须保存，Worker 后续只使用冻结结果。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from agent.product_catalog import ProductCatalogService

logger = logging.getLogger("backend.product_matcher")

# 每个产品的关键词（legacy 兜底；Catalog 已收敛 keywords，优先使用 catalog）
_PRODUCT_KEYWORDS: dict[str, list[str]] = {
    "agent-identity-security": [
        "智能体身份",
        "agent身份",
        "身份认证",
        "授权",
        "最小权限",
        "agent identity",
        "权限边界",
        "身份治理",
    ],
    "agent-security": [
        "智能体安全",
        "agent安全",
        "agent防护",
        "智能体防护",
        "agent runtime",
        "智能体运行时",
        "agent检测",
    ],
    "ai-bom": [
        "AI-BOM",
        "AI物料清单",
        "AI资产",
        "供应链安全",
        "AI组件",
        "模型供应链",
        "AI bill of materials",
    ],
    "agent-security-gateway": [
        "安全网关",
        "API网关",
        "流量管控",
        "agent网关",
    ],
    "ans": [
        "ANS",
        "亚信安全网络服务",
        "网络安全服务",
    ],
}

# 通用弱词：命中这些词不构成强产品信号，权重会被压低
_WEAK_TERMS: frozenset[str] = frozenset(
    {
        "供应链安全",
        "安全",
        "防护",
        "检测",
        "治理",
        "风险",
        "合规",
        "数据",
        "模型",
        "组件",
        "AI",
        "智能体",
    }
)

# 分字段权重：标题 > 摘要 > 正文
_FIELD_WEIGHT = {"title": 30, "summary": 20, "content": 10}
_ALIAS_WEIGHT = {"title": 40, "summary": 25, "content": 15}
_WEAK_WEIGHT = 5

# 最大参与匹配的正文长度（字符）
_MAX_TEXT_LEN = 20_000

# 高置信阈值 / 歧义分差阈值
CONFIDENCE_HIGH = 60
AMBIGUITY_GAP = 15

# 路由弱匹配过滤分差：次优产品与最高分产品分差超过该值时视为噪声弱匹配，
# 不应进入正式路由结果（避免命中禁止产品造成误隔离）。
# 值需小于单关键词 title 命中 30 与弱词单字段命中 20 的差，确保仅过滤弱词噪声。
MIN_MATCH_GAP = 25


@dataclass(frozen=True)
class ProductMatch:
    """单个产品匹配结果。"""

    product_id: str
    product_name: str
    match_score: int
    match_reason: str


def _normalize(text: str) -> str:
    """空白归一化：去掉所有空白并小写，缓解中英混排空格导致的召回断链。"""
    return re.sub(r"\s+", "", text).lower()


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
            article: 文章字典（title, summary_cn/summary, content_md/content, ...）
            top_n: 返回的最多产品数
            user_products: 用户级产品列表，每项包含
                product_id, name, aliases, keywords

        Returns:
            按匹配分数降序排列的 ProductMatch 列表
        """
        fields = self._field_texts(article)
        if not any(fields.values()):
            return []

        scores: list[tuple[str, str, int, list[str]]] = []

        # 全局产品匹配（仅已发布产品进入正式 auto 路由）
        published_products = self._catalog.list_products(published_only=True)
        for product in published_products:
            keywords = product.keywords or _PRODUCT_KEYWORDS.get(product.product_id, [])
            score, hits = self._score_entry(
                keywords=keywords,
                aliases=product.aliases,
                fields=fields,
            )
            if hits:
                scores.append((product.product_id, product.name, score, hits))

        # 用户级产品匹配
        if user_products:
            combined = " ".join(v for v in fields.values() if v)
            combined_norm = _normalize(combined)
            for up in user_products:
                pid = up.get("product_id", "")
                pname = up.get("name", pid)
                aliases = up.get("aliases", [])
                keywords = up.get("keywords", [])
                hits = [kw for kw in keywords if _normalize(kw) and _normalize(kw) in combined_norm]
                for alias in aliases:
                    if _normalize(alias) and _normalize(alias) in combined_norm:
                        hits.append(alias)
                if hits:
                    score = min(len(hits) * 20, 100)
                    scores.append((pid, pname, score, hits[:5]))

        if not scores:
            return []

        # 过滤低置信弱匹配：当多个产品命中且次优产品与最高分产品分差过大时，
        # 次优产品多为弱词噪声（如仅 title 单字段命中的弱词约 20 分），不应进入
        # 正式路由结果，否则会被判定为"命中禁止产品"造成误隔离。
        # 采用"相对分差"而非绝对阈值：保底保留最高分产品（唯一合法信号时仍需返回），
        # 仅过滤与第一名分差超过 MIN_MATCH_GAP 的次优/后续弱匹配。
        filtered = sorted(scores, key=lambda x: x[2], reverse=True)
        if len(filtered) > 1:
            top = filtered[0][2]
            filtered = [s for s in filtered if s[2] >= top or (top - s[2]) <= MIN_MATCH_GAP]

        results: list[ProductMatch] = []
        for pid, pname, score, hits in filtered[:top_n]:
            results.append(
                ProductMatch(
                    product_id=pid,
                    product_name=pname,
                    match_score=min(score, 100),
                    match_reason=f"匹配关键词: {', '.join(hits[:3])}",
                )
            )

        return results

    @staticmethod
    def is_ambiguous(matches: list[ProductMatch]) -> bool:
        """判定路由结果是否歧义。

        满足以下任一条件视为歧义：
        - Top1 分数低于高置信阈值；
        - Top1 与 Top2 分差过小。
        """
        if not matches:
            return True
        if matches[0].match_score < CONFIDENCE_HIGH:
            return True
        return (
            len(matches) >= 2 and (matches[0].match_score - matches[1].match_score) < AMBIGUITY_GAP
        )

    @staticmethod
    def _score_entry(
        *,
        keywords: tuple[str, ...] | list[str],
        aliases: tuple[str, ...],
        fields: dict[str, str],
    ) -> tuple[int, list[str]]:
        """对单个产品打分：分字段加权 + 别名加权 + 弱词降权。"""
        score = 0
        hits: list[str] = []

        for raw in keywords:
            kw = _normalize(raw)
            if not kw:
                continue
            field_hits = 0
            for field, weight in _FIELD_WEIGHT.items():
                if kw in fields[field]:
                    field_hits += weight
            if field_hits == 0:
                continue
            if kw in _WEAK_TERMS:
                # 弱词即使命中多个字段也保持低权重，避免单独形成高置信
                field_hits = min(field_hits, _WEAK_WEIGHT)
            score += field_hits
            hits.append(str(raw))

        for raw_alias in aliases:
            alias = _normalize(raw_alias)
            if not alias:
                continue
            for field, weight in _ALIAS_WEIGHT.items():
                if alias in fields[field]:
                    score += weight
                    hits.append(str(raw_alias))
                    break  # 每个别名只在一个字段计一次（优先级 title>summary>content）

        return score, hits

    @staticmethod
    def _field_texts(article: dict[str, Any]) -> dict[str, str]:
        """按阶段1 目标字段优先级提取并归一化各字段文本。

        优先级：title → summary_cn → summary → content_md → content
        （category_v2 / tags / source 作为辅助信号并入正文侧）。
        """
        title = article.get("title") or ""
        summary = article.get("summary_cn") or article.get("summary") or ""
        content = article.get("content_md") or article.get("content") or ""

        # 辅助信号（标签/分类/来源）并入正文，增强召回
        extras: list[str] = []
        for key in ("category_v2", "source"):
            val = article.get(key)
            if isinstance(val, str) and val:
                extras.append(val)
        tags = article.get("tags")
        if isinstance(tags, list):
            extras.extend(str(t) for t in tags if t)

        content = content[:_MAX_TEXT_LEN]
        if extras:
            content = content + " " + " ".join(extras)

        return {
            "title": _normalize(title),
            "summary": _normalize(summary),
            "content": _normalize(content),
        }

    @staticmethod
    def _extract_text(article: dict[str, Any]) -> str:
        """兼容旧接口：拼接收敛后的全部文本。"""
        fields = ProductMatcher._field_texts(article)
        return " ".join(v for v in fields.values() if v)

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
