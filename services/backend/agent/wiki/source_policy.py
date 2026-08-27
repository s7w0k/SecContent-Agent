"""Source Priority 集中配置（Phase 3 / PR-10，§6.4）。

- 优先级集中定义，不散落在代码：Official technical docs > Official datasheet
  > Official marketing > Trusted third party > LLM synthesis
- 用于冲突裁决：相同语义下，高优先级来源的 Claim 被选为 current；
  但历史冲突必须保留（status=conflicted），不能静默覆盖（§6.3）。
"""

from __future__ import annotations

# §6.4 推荐的来源优先级（高度可信 → 低可信）
SOURCE_PRIORITY_ORDER: list[str] = [
    "official_technical_docs",
    "official_datasheet",
    "official_marketing",
    "trusted_third_party",
    "llm_synthesis",
]
_PRIORITY_RANK: dict[str, int] = {kind: i for i, kind in enumerate(SOURCE_PRIORITY_ORDER)}


class SourcePolicy:
    """来源优先级策略。kind_map 可把自有来源分类映射到标准优先级。"""

    def __init__(self, order: list[str] | None = None, kind_map: dict[str, str] | None = None):
        self.order = list(order) if order else list(SOURCE_PRIORITY_ORDER)
        self._rank = {kind: i for i, kind in enumerate(self.order)}
        self.kind_map = dict(kind_map or {})

    def canonical_kind(self, source_kind: str) -> str:
        return self.kind_map.get(source_kind, source_kind)

    def rank(self, source_kind: str) -> int:
        """越小越可信；未知来源归为最低优先级（llm_synthesis）。"""
        kind = self.canonical_kind(source_kind)
        return self._rank.get(kind, len(self.order))

    def is_higher(self, a: str, b: str) -> bool:
        """来源 a 是否比来源 b 更可信（tie 返回 False）。"""
        return self.rank(a) < self.rank(b)

    def best(self, sources: list[str]) -> str | None:
        """返回一组来源中优先级最高者；空列表返回 None。"""
        if not sources:
            return None
        return min(sources, key=self.rank)


DEFAULT_SOURCE_POLICY = SourcePolicy()

# 来源类型分类别名（便于 Raw Source 声明其来源性质）
SOURCE_KIND_ALIASES: dict[str, str] = {
    "release_notes": "official_datasheet",
    "datasheet": "official_datasheet",
    "product_doc": "official_technical_docs",
    "official_doc": "official_technical_docs",
    "marketing": "official_marketing",
    "blog": "official_marketing",
    "partner": "trusted_third_party",
    "third_party": "trusted_third_party",
    "llm": "llm_synthesis",
    "synthesis": "llm_synthesis",
}

__all__ = [
    "DEFAULT_SOURCE_POLICY",
    "SOURCE_KIND_ALIASES",
    "SOURCE_PRIORITY_ORDER",
    "_PRIORITY_RANK",
    "SourcePolicy",
]
