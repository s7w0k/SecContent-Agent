"""Requirement Evaluator - 用"经过验证的 Evidence"计算 Requirement Coverage（Final PR-B）。

取代旧的"打开某 page_type → Requirement MET"的 Page-count Coverage：
  - 只有 reason_code == VERIFIED 且 confidence / relevance 达标的 Evidence 才能满足 Requirement
  - Conflict / Stale / Unsupported 证据不计入满足度
  - Coverage 按 Requirement 权重加权：Σ(MET 权重)/Σ(全部权重)
  - duplicated evidence 按 evidence_id 去重，不重复计数
  - 支持 required 必须需求：全部满足才算 SUFFICIENT 前提

链路：Navigator → Collector → Verifier → RequirementEvaluator → EvidenceBundle
"""

from __future__ import annotations

from typing import Any, Literal

from agent.wiki.requirements import EvidenceRequirement
from pydantic import BaseModel, Field

RequirementStatus = Literal["OPEN", "PARTIAL", "MET", "CONFLICTED"]

# 不计入满足度的 reason_code（§5.8）
_NON_COUNTED_REASON_CODES = frozenset(
    {
        "SOURCE_HASH_MISMATCH",
        "STALE_SOURCE_REF",
        "SOURCE_MISSING",
        "SECTION_NOT_FOUND",
        "NOT_SUPPORTED",
        "CONTRADICTED",
    }
)


class RequirementResult(BaseModel):
    """单个 Requirement 的评估结果（§5.5）。"""

    requirement_id: str
    description: str
    weight: float
    status: RequirementStatus
    required: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_count: int = Field(default=0)
    confidence: float = Field(default=0.0)


class RequirementEvaluation(BaseModel):
    """一次任务的 Requirement 总评（供 EvidenceBundle 使用）。"""

    results: list[RequirementResult] = Field(default_factory=list)
    coverage: float = Field(default=0.0)
    confidence: float = Field(default=0.0)
    met_requirements: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    all_required_met: bool = Field(default=False)

    def is_sufficient(
        self,
        *,
        min_coverage: float = 0.7,
        confidence_threshold: float = 0.8,
        no_blocking_conflict: bool = True,
    ) -> bool:
        """GOAL A/§9：`sufficient` 必须由 Verified Evidence 状态硬校验决定。

        由 `all_required_met` + coverage 达标 + confidence 达标 + 无阻断冲突 共同决定，
        不允许由 LLM / page_type 自行声称"证据够了"。
        """
        return bool(
            self.all_required_met
            and self.coverage >= min_coverage
            and self.confidence >= confidence_threshold
            and no_blocking_conflict
        )


class RequirementEvaluator:
    """把已验证证据映射为 Requirement，并计算加权 Coverage（§5.2-§5.7）。"""

    def __init__(
        self,
        *,
        confidence_threshold: float = 0.8,
        relevance_threshold: float = 0.5,
    ):
        self.confidence_threshold = confidence_threshold
        self.relevance_threshold = relevance_threshold

    def evaluate(
        self,
        requirements: list[EvidenceRequirement],
        evidence: list[Any],
        conflicts: list[Any] | None = None,
    ) -> RequirementEvaluation:
        """评估。evidence: 已通过 Verifier 的 EvidenceItem 列表。"""
        reqs = list(requirements or [])
        results: list[RequirementResult] = []
        conflicted_facts, conflicted_evidence_ids = self._conflict_facts(conflicts)

        total_weight = sum(r.weight for r in reqs) or 1.0
        earned_weight = 0.0
        met_ids: list[str] = []
        missing_ids: list[str] = []
        all_confidences: list[float] = []

        for r in reqs:
            pool = [e for e in evidence if r.requirement_id in (e.requirement_ids or [])]
            # 冲突证据：该 Requirement 涉入冲突 → 标记 CONFLICTED，不计入满足
            conflicted = any(
                (e.fact in conflicted_facts or e.evidence_id in conflicted_evidence_ids)
                for e in pool
            )
            valid = self._valid_evidence(pool)

            if conflicted:
                status: RequirementStatus = "CONFLICTED"
            elif len(valid) >= r.minimum_evidence:
                status = "MET"
            elif valid:
                status = "PARTIAL"
            else:
                status = "OPEN"

            conf = self._mean([e.confidence for e in valid])
            if valid:
                all_confidences.extend(e.confidence for e in valid)

            if status == "MET":
                earned_weight += r.weight
                met_ids.append(r.requirement_id)
            else:
                missing_ids.append(r.requirement_id)

            results.append(
                RequirementResult(
                    requirement_id=r.requirement_id,
                    description=r.description,
                    weight=r.weight,
                    status=status,
                    required=r.required,
                    evidence_ids=sorted({e.evidence_id for e in valid}),
                    evidence_count=len({e.evidence_id for e in valid}),
                    confidence=round(conf, 4),
                )
            )

        coverage = earned_weight / total_weight
        all_required_met = all(
            rs.status == "MET" for rs in results if rs.required
        )
        return RequirementEvaluation(
            results=results,
            coverage=round(coverage, 4),
            confidence=round(self._mean(all_confidences), 4),
            met_requirements=met_ids,
            missing_requirements=missing_ids,
            all_required_met=all_required_met,
        )

    # ── 内部 ──────────────────────────────────────────────

    def _valid_evidence(self, pool: list[Any]) -> list[Any]:
        """只有 VERIFIED 且 confidence/relevance 达标、且非 conflict/stale 的证据才算有效。"""
        return [
            e
            for e in pool
            if e.reason_code == "VERIFIED"
            and e.reason_code not in _NON_COUNTED_REASON_CODES
            and e.confidence >= self.confidence_threshold
            and e.relevance >= self.relevance_threshold
        ]

    @staticmethod
    def _conflict_facts(conflicts: list[Any] | None) -> tuple[set[str], set[str]]:
        facts: set[str] = set()
        evidence_ids: set[str] = set()
        for c in conflicts or []:
            facts.update(getattr(c, "claims", []) or [])
            evidence_ids.update(getattr(c, "evidence_ids", []) or [])
        return facts, evidence_ids

    @staticmethod
    def _mean(values: list[float]) -> float:
        if not values:
            return 0.0
        return sum(values) / len(values)
