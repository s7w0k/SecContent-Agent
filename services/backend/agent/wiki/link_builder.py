"""Link Builder - 构建 Wiki 页面间关系链接。

PR-03 第三步（两阶段）：
  - Pass 1：确定性链接（基于 page_id / product_id / page_type）
  - Pass 2：LLM Suggested Link（必须通过 target exists / relation allowed / no self loop 校验）

设计约束：
  - 目标页必须存在才建立链接；禁止自循环；关系类型白名单
"""

from __future__ import annotations

from typing import Any

from agent.wiki.contracts import WikiPage, WikiRelation
from agent.wiki.store import WikiStore

ALLOWED_RELATIONS = frozenset(
    {
        "belongs_to",
        "related_to",
        "mitigates",
        "addresses",
        "extends",
        "implements",
        "part_of",
        "requires",
        "competes_with",
        "used_in",
    }
)


class LinkBuilder:
    """页面链接构建器。"""

    def __init__(
        self,
        store: WikiStore,
        allowed_relations: frozenset[str] = ALLOWED_RELATIONS,
        llm: Any | None = None,
    ):
        self.store = store
        self.allowed = allowed_relations
        self._llm = llm

    # ── Pass 1：确定性链接 ────────────────────────────────

    def deterministic_relations(self, page: WikiPage) -> list[WikiRelation]:
        relations: list[WikiRelation] = []
        page_id = page.meta.page_id
        segments = page_id.split(".")

        # 归属产品索引：product.X.capability.y → belongs_to product.X
        if segments[0] == "product" and len(segments) >= 2 and len(segments) > 2:
            product_page = "product." + segments[1]
            if product_page != page_id and self.store.page_exists(product_page):
                relations.append(
                    WikiRelation(relation_type="belongs_to", target_page_id=product_page)
                )

        # 子类型页 → 产品 overview
        if segments[0] == "product" and len(segments) == 4:
            product_root = "product." + segments[1]
            overview = f"{product_root}.overview"
            if overview != page_id and self.store.page_exists(overview):
                relations.append(WikiRelation(relation_type="related_to", target_page_id=overview))

        # 产品索引 → overview / positioning / capabilities 清单
        if segments[0] == "product" and len(segments) == 2:
            base = page_id
            for sub in ("overview", "positioning"):
                target = f"{base}.{sub}"
                if self.store.page_exists(target):
                    relations.append(
                        WikiRelation(relation_type="related_to", target_page_id=target)
                    )
            # 关联能力页
            for child in self.store.list_page_ids():
                if child.startswith(base + ".capability."):
                    relations.append(WikiRelation(relation_type="related_to", target_page_id=child))
        return _dedupe(relations)

    # ── Pass 2：LLM 建议链接（校验）───────────────────────

    def validate_suggestion(self, page_id: str, relation_type: str, target: str) -> bool:
        if not relation_type or relation_type not in self.allowed:
            return False
        if not target or target == page_id:
            return False
        return self.store.page_exists(target)

    def build(
        self, page: WikiPage, llm_suggestions: list[dict] | None = None
    ) -> list[WikiRelation]:
        """合并确定性关系与通过校验的 LLM 建议链接，返回最终关系列表。"""
        merged = self.deterministic_relations(page)

        for item in llm_suggestions or []:
            rtype = str(item.get("type") or item.get("relation_type") or "")
            target = str(item.get("target") or item.get("target_page_id") or "")
            if self.validate_suggestion(page.meta.page_id, rtype, target):
                merged.append(WikiRelation(relation_type=rtype, target_page_id=target))

        return _dedupe(merged)


def _dedupe(relations: list[WikiRelation]) -> list[WikiRelation]:
    seen: set[tuple[str, str]] = set()
    out: list[WikiRelation] = []
    for r in relations:
        key = (r.relation_type, r.target_page_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
