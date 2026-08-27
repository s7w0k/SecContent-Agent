"""Entity Resolver - 实体解析流水线（Phase 6 / PR-12，§9）。

Resolution Pipeline（文档 §9.1）：
  1 exact product_id
  2 exact canonical name
  3 exact alias
  4 normalized alias
  5 bounded fuzzy alias
  6 lexical/FTS search
  7 optional LLM disambiguation（本实现用确定性 Ambiguity Contract 替代）

返回 EntityCandidate + ResolutionResult（RESOLVED / AMBIGUOUS_ENTITY / UNKNOWN_ENTITY）。

Ambiguity Contract（§9.2）：
  top1.score >= 0.90 AND top1.score - top2.score >= 0.15 → RESOLVED
  否则有候选 → AMBIGUOUS_ENTITY；无候选 → UNKNOWN_ENTITY

关键约束（§9.3）：
  不再存在 “no entity → every product page” 的全产品兜底，
  替换为有界 descriptor 搜索 + 消歧 + 未决则 UNKNOWN_ENTITY。
"""

from __future__ import annotations

import difflib
import logging
import unicodedata
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.wiki.resolver")

# 消歧阈值（§9.2）
RESOLVE_THRESHOLD = 0.90
AMBIGUITY_MARGIN = 0.15
# 有界 fuzzy 搜索的最大候选数与相似度下限
FUZZY_CUTOFF = 0.80
FUZZY_MAX = 3
# 有界 descriptor 搜索的最大结果
DESCRIPTOR_MAX = 5

# 各 match_type 的固定评分
_SCORE_PRODUCT_ID = 1.00
_SCORE_CANONICAL = 0.98
_SCORE_ALIAS = 0.95
_SCORE_NORMALIZED_ALIAS = 0.90


class MatchType(StrEnum):
    """实体命中来源类型。"""

    PRODUCT_ID = "product_id"
    CANONICAL = "canonical_name"
    ALIAS = "exact_alias"
    NORMALIZED = "normalized_alias"
    FUZZY = "fuzzy_alias"
    LEXICAL = "lexical_search"


class EntityStatus(StrEnum):
    """解析结果状态。"""

    RESOLVED = "RESOLVED"
    AMBIGUOUS_ENTITY = "AMBIGUOUS_ENTITY"
    UNKNOWN_ENTITY = "UNKNOWN_ENTITY"


class EntityCandidate(BaseModel):
    """单个候选实体。"""

    page_id: str
    entity_id: str = Field(description="canonical entity key，通常为 product_id 或 page_id")
    score: float = Field(ge=0.0, le=1.0)
    match_type: str = Field(description="MatchType 值")
    matched_alias: str = Field(default="")


class ResolutionResult(BaseModel):
    """一次实体解析的完整结果。"""

    status: EntityStatus
    query: str
    candidates: list[EntityCandidate] = Field(default_factory=list)

    @property
    def top(self) -> EntityCandidate | None:
        return self.candidates[0] if self.candidates else None

    def is_resolved(self) -> bool:
        return self.status == EntityStatus.RESOLVED


def normalize_text(text: str) -> str:
    """中文/全角友好的归一化：NFKC 全角→半角 + casefold + 折叠空白。

    用于 normalized alias 比较与模糊匹配（§9.4 中文检索依赖）。
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text))
    return " ".join(folded.casefold().split())


class EntityResolver:
    """确定性实体解析器。既支持 WikiIndex 也支持退化为 Store 扫描。"""

    def __init__(
        self,
        index: Any | None = None,
        store: Any | None = None,
        *,
        resolve_threshold: float = RESOLVE_THRESHOLD,
        ambiguity_margin: float = AMBIGUITY_MARGIN,
    ):
        self.index = index
        self.store = store
        self.resolve_threshold = resolve_threshold
        self.ambiguity_margin = ambiguity_margin

    # ── 实体收集 ──────────────────────────────────────────

    def _catalog(self) -> list[tuple[str, str, str, list[str]]]:
        """产出 (page_id, entity_id, title, aliases)。entity_id 优先取 product_id。"""
        pages = self._iter_pages()
        cat: list[tuple[str, str, str, list[str]]] = []
        for p in pages:
            page_id = _page_id(p)
            entity_id = _product_id(p) or page_id
            cat.append((page_id, entity_id, _title(p), _aliases(p)))
        return cat

    def _iter_pages(self) -> Iterable[Any]:
        if self.index is not None and getattr(self.index, "manifest", None) is not None:
            yield from self.index.manifest.pages
            return
        if self.store is not None:
            yield from self.store.list_pages()
            return
        return iter([])

    # ── 解析入口 ──────────────────────────────────────────

    def resolve(self, name: str) -> ResolutionResult:
        """运行完整解析流水线，返回带状态的 ResolutionResult。"""
        query = (name or "").strip()
        if not query:
            return ResolutionResult(status=EntityStatus.UNKNOWN_ENTITY, query=query, candidates=[])

        hits: dict[str, EntityCandidate] = {}
        for page_id, entity_id, title, aliases in self._catalog():
            self._pipeline_against_entity(hits, query, page_id, entity_id, title, aliases)

        return self._finalize(query, hits)

    def _pipeline_against_entity(
        self,
        hits: dict[str, EntityCandidate],
        query: str,
        page_id: str,
        entity_id: str,
        title: str,
        aliases: list[str],
    ) -> None:
        norm_q = normalize_text(query)
        norm_entity = normalize_text(entity_id)
        has_exact = False

        # 1 exact product_id
        if norm_q == norm_entity:
            self._add(hits, page_id, entity_id, _SCORE_PRODUCT_ID, MatchType.PRODUCT_ID, query)
            has_exact = True
        # 2 exact canonical name
        elif norm_q == normalize_text(title):
            self._add(hits, page_id, entity_id, _SCORE_CANONICAL, MatchType.CANONICAL, title)
            has_exact = True
        # 3 exact alias
        exact_alias = next((a for a in aliases if a == query), None)
        if exact_alias is not None:
            self._add(hits, page_id, entity_id, _SCORE_ALIAS, MatchType.ALIAS, exact_alias)
            has_exact = True
        # 4 normalized alias
        norm_alias = next((a for a in aliases if norm_q == normalize_text(a)), None)
        if norm_alias is not None:
            self._add(
                hits,
                page_id,
                entity_id,
                _SCORE_NORMALIZED_ALIAS,
                MatchType.NORMALIZED,
                norm_alias,
            )
            has_exact = True
        # 5 bounded fuzzy alias（仅当未精确命中，避免 ratio=1.0 误标为 fuzzy）
        if not has_exact:
            fuzzy = difflib.get_close_matches(
                norm_q, [normalize_text(a) for a in aliases], n=FUZZY_MAX, cutoff=FUZZY_CUTOFF
            )
            if fuzzy:
                ratio = difflib.SequenceMatcher(None, norm_q, fuzzy[0]).ratio()
                self._add(hits, page_id, entity_id, ratio, MatchType.FUZZY, fuzzy[0])
        # 6 lexical / FTS search
        if self.index is not None and getattr(self.index, "search", None):
            for entry in self.index.search(query, limit=5):
                if entry.page_id == page_id:
                    self._add(hits, page_id, entity_id, 0.6, MatchType.LEXICAL, query)

    def _finalize(self, query: str, hits: dict[str, EntityCandidate]) -> ResolutionResult:
        # 每个 entity 只保留一个最优候选：同分时优先 canonical(product) 页，
        # 避免"按 product_id 命中全产品子页"被误判为歧义。
        by_entity: dict[str, EntityCandidate] = {}
        for c in sorted(hits.values(), key=lambda c: (-c.score, c.page_id)):
            prev = by_entity.get(c.entity_id)
            if (
                prev is None
                or c.score > prev.score
                or (c.score == prev.score and _is_canonical_page(c))
            ):
                by_entity[c.entity_id] = c

        candidates = sorted(by_entity.values(), key=lambda c: (-c.score, c.page_id))
        if not candidates:
            return ResolutionResult(status=EntityStatus.UNKNOWN_ENTITY, query=query, candidates=[])
        top1 = candidates[0]
        top2 = candidates[1] if len(candidates) > 1 else None
        margin = (top1.score - top2.score) if top2 else self.ambiguity_margin
        resolved = top1.score >= self.resolve_threshold and margin >= self.ambiguity_margin
        status = EntityStatus.RESOLVED if resolved else EntityStatus.AMBIGUOUS_ENTITY
        return ResolutionResult(status=status, query=query, candidates=candidates)

    @staticmethod
    def _add(
        hits: dict[str, EntityCandidate],
        page_id: str,
        entity_id: str,
        score: float,
        match_type: MatchType,
        matched_alias: str,
    ) -> None:
        prev = hits.get(page_id)
        if prev is None or score > prev.score:
            hits[page_id] = EntityCandidate(
                page_id=page_id,
                entity_id=entity_id,
                score=score,
                match_type=match_type.value,
                matched_alias=matched_alias,
            )


# ── page 字段访问（兼容 index 条目与 store meta）───────────
# 传入 catalog 的统一对象可能是 WikiPageIndex 或 WikiPageMeta。
# 用安全 getattr 避免依赖具体类型。


def _page_id(p: Any) -> str:
    return str(getattr(p, "page_id", ""))


def _product_id(p: Any) -> str | None:
    v = getattr(p, "product_id", None)
    return str(v) if v else None


def _title(p: Any) -> str:
    return str(getattr(p, "title", ""))


def _aliases(p: Any) -> list[str]:
    return list(getattr(p, "aliases", []) or [])


def _is_canonical_page(c: EntityCandidate) -> bool:
    """是否产品 canonical 页：page_id 形如 `product.<pid>` 或无子类型段。"""
    segs = c.page_id.split(".")
    return len(segs) == 2 and segs[0] == "product"
