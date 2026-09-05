"""Evidence Contract - 业务 Agent 与知识系统之间的硬边界。

PR-06 产物：
  - EvidenceItem：单条带 provenance 的事实声明
  - EvidenceConflict：冲突
  - EvidenceBundle：一次业务任务的知识产出（只读 Wiki 的结果）

边界约定：
  Wiki Runtime → EvidenceBundle →(硬边界)→ Business Reasoning
  禁止 ScoringAgent / DraftAgent / ChatAgent 直接访问整个 Wiki 根目录。
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from agent.wiki.contracts import SourceRef
from pydantic import BaseModel, Field

BundleStatus = Literal["SUFFICIENT", "INSUFFICIENT_EVIDENCE", "CONFLICTED", "FAILED"]


def stable_evidence_id(
    *,
    page_id: str,
    claim_id: str = "",
    fact: str = "",
    source_refs: list[Any] | None = None,
) -> str:
    """稳定 Evidence ID：同一 Claim/事实 + 同一页面 + 同一 Canonical Source 恒为同一 ID。

    PR-1.2：增量导航期间同一 Claim 在多轮出现必须得到同一 evidence_id，
    以保证 dedupe / minimum_evidence / coverage / trace / replay 的一致性。
      - 有 claim_id：sha256(claim_id + page_id + canonical_source_refs)[:20]
      - 无 claim_id：sha256(normalized_fact + page_id + canonical_source_refs)[:20]

    canonical_source_refs 使用 (source_id, content_hash) 排序后的稳定串，
    避免 SourceRef 中路径/章节等非稳定字段造成 ID 漂移。
    """
    from agent.wiki.resolver import normalize_text

    refs_parts = sorted(
        f"{getattr(r, 'source_id', '')}:{getattr(r, 'content_hash', '')}"
        for r in (source_refs or [])
    )
    refs_blob = "|".join(refs_parts)
    if claim_id:
        key = f"{claim_id}|{page_id}|{refs_blob}"
    else:
        key = f"{normalize_text(fact)}|{page_id}|{refs_blob}"
    return "ev:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


class EvidenceItem(BaseModel):
    """单条证据声明。"""

    evidence_id: str = Field(description="稳定证据 ID（如 ev-1）")
    fact: str = Field(description="事实陈述")
    claim_id: str = Field(default="", description="关联的结构化 Claim ID（Phase3，G-12）")

    page_id: str = Field(description="来源 Wiki 页面")
    page_title: str = Field(default="")

    section_id: str = Field(default="", description="来源章节 ID（§12.2，Verifier V2 优先）")
    requirement_ids: list[str] = Field(default_factory=list)
    reason_code: str = Field(default="PENDING", description="Verifier V2 reason code（§12.5）")

    source_refs: list[SourceRef] = Field(default_factory=list)

    relevance: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=0.0, ge=0, le=1)

    relation_to_task: str = Field(default="", description="该事实与任务的关系说明")


class EvidenceConflict(BaseModel):
    """证据冲突（两个高优先级来源矛盾）。"""

    topic: str = Field(description="冲突主题")
    claims: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


class EvidenceBundle(BaseModel):
    """一次业务任务的知识产出。"""

    task_type: str = Field(default="score")
    query: str = Field(default="")

    product_ids: list[str] = Field(default_factory=list)

    evidence: list[EvidenceItem] = Field(default_factory=list)

    coverage: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=0.0, ge=0, le=1)

    conflicts: list[EvidenceConflict] = Field(default_factory=list)

    visited_pages: list[str] = Field(default_factory=list)
    wiki_version: str = Field(default="")

    # PR-B（V2）：按 Requirement 评估的结果（替代 Page-count Coverage）
    requirements: list[dict] = Field(
        default_factory=list, description="RequirementResult dict 列表"
    )
    missing_requirements: list[str] = Field(default_factory=list)

    status: BundleStatus = Field(default="FAILED")

    def verified(self, min_confidence: float = 0.8) -> list[EvidenceItem]:
        return [e for e in self.evidence if e.confidence >= min_confidence]

    def is_sufficient(self) -> bool:
        return self.status == "SUFFICIENT"
