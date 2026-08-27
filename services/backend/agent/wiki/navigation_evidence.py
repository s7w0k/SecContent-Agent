"""Live Evidence Session - 把"增量导航"与"已验证证据状态"合一（Goal A / PR-1）。

背景（前几版根因，§3）：
  - Navigator Requirement State = Page-driven（tracker.observe_page）
  - Final Bundle Requirement State = Verified-Evidence-driven（Provider 收尾才算）

本模块新增 `NavigationEvidenceSession`：由 `WikiKnowledgeProvider` 创建并注入
Navigator。每打开一个新 Wiki Page 后**立即**做增量 `Collect → Verify → Evaluate`，
产出 **request-scoped** 的 `NavigationEvidenceSnapshot`。Navigator 只消费这个
Snapshot，不再用 page_type 直接判定 Requirement MET（§4）。

三个职责（§8）：
  1. `NavigationEvidenceSnapshot`：导航与最终 Bundle 共用的单一事实源
  2. `EvidenceAccumulator`：按稳定 evidence_id 合并，防重复计数
  3. `NavigationEvidenceSession`：每轮 assess_page 增量更新快照

并发约束（§8/§15）：Session 必须每个 `KnowledgeRequest` 新建一次，
禁止挂在全局 Provider / Navigator 上共享可变状态。
"""

from __future__ import annotations

from typing import Any

from agent.wiki.conflict_detector import detect_conflicts
from agent.wiki.evidence import EvidenceConflict, EvidenceItem
from agent.wiki.requirement_evaluator import (
    RequirementEvaluator,
    RequirementResult,
)
from pydantic import BaseModel, Field


class NavigationEvidenceSnapshot(BaseModel):
    """一次导航当前"已验证证据状态"的快照。

    单一事实源：既用于 LLM 导航决策上下文，也用于最终 EvidenceBundle 组装。
    """

    evidence: list[EvidenceItem] = Field(default_factory=list)
    requirements: list[RequirementResult] = Field(default_factory=list)
    met_requirements: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    coverage: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=0.0, ge=0, le=1)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
    all_required_met: bool = Field(default=False)
    sufficient: bool = Field(default=False)


class EvidenceAccumulator:
    """按稳定 evidence_id 合并证据（§8）：相同 ID 永远只算一份。"""

    def __init__(self) -> None:
        self._items: dict[str, EvidenceItem] = {}

    def merge(self, items: list[EvidenceItem] | None) -> None:
        for item in items or []:
            current = self._items.get(item.evidence_id)
            if current is None:
                self._items[item.evidence_id] = item
                continue
            merged = self._prefer(current, item)
            if merged is not None:
                self._items[item.evidence_id] = merged

    def values(self) -> list[EvidenceItem]:
        return list(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    @staticmethod
    def _prefer(a: EvidenceItem, b: EvidenceItem) -> EvidenceItem | None:
        """合并规则（§8）：VERIFIED > 非 VERIFIED；更高 confidence；更高 relevance；fresh > stale。

        返回被采纳者；返回 None 表示维持原状。
        """
        a_ver = a.reason_code == "VERIFIED"
        b_ver = b.reason_code == "VERIFIED"
        if a_ver != b_ver:
            return a if a_ver else b
        if abs(a.confidence - b.confidence) > 1e-9:
            return a if a.confidence > b.confidence else b
        if abs(a.relevance - b.relevance) > 1e-9:
            return a if a.relevance > b.relevance else b
        # 相等时保留先到的（更"新鲜"的增量结果一般等价；此处不替换）
        return a


class NavigationEvidenceSession:
    """request-scoped 的导航证据会话：每开一页增量评估，产出最新快照。

    运行流程（§9）：
      collector.collect_page(new page)
        → verifier.verify(new candidates)
        → accumulator.merge(verified)
        → detect_conflicts(all accumulated evidence)
        → RequirementEvaluator.evaluate(...)
        → NavigationEvidenceSnapshot
    """

    def __init__(
        self,
        *,
        collector: Any,
        verifier: Any,
        evaluator: RequirementEvaluator,
        task_requirements: list[Any],
        min_coverage: float = 0.7,
        confidence_threshold: float = 0.8,
        query: str = "",
        task_type: str = "score",
    ):
        self._collector = collector
        self._verifier = verifier
        self._evaluator = evaluator
        self._requirements = list(task_requirements or [])
        self._min_coverage = min_coverage
        self._confidence_threshold = confidence_threshold
        self._query = query
        self._task_type = task_type
        self._accumulator = EvidenceAccumulator()
        self._opened: dict[str, Any] = {}

    def initial_snapshot(self) -> NavigationEvidenceSnapshot:
        """没有任何页面时的初始快照（用于导航循环起点）。"""
        return self._evaluate()

    def assess_page(
        self,
        *,
        page_id: str,
        page: Any,
    ) -> NavigationEvidenceSnapshot:
        """只对**新打开的一页**做增量 Collect → Verify → Merge → Evaluate。"""
        new_candidates = self._collector.collect_page(
            query=self._query,
            page_id=page_id,
            page=page,
            task_type=self._task_type,
        )
        verified = self._verifier.verify(new_candidates)
        self._accumulator.merge(verified)
        self._opened[page_id] = page
        return self._evaluate()

    def _evaluate(self) -> NavigationEvidenceSnapshot:
        evidence = self._accumulator.values()
        conflicts = detect_conflicts(evidence)
        evaluation = self._evaluator.evaluate(
            self._requirements, evidence, conflicts=conflicts
        )
        sufficient = evaluation.is_sufficient(
            min_coverage=self._min_coverage,
            confidence_threshold=self._confidence_threshold,
            no_blocking_conflict=not conflicts,
        )
        return NavigationEvidenceSnapshot(
            evidence=evidence,
            requirements=evaluation.results,
            met_requirements=evaluation.met_requirements,
            missing_requirements=evaluation.missing_requirements,
            coverage=evaluation.coverage,
            confidence=evaluation.confidence,
            conflicts=conflicts,
            all_required_met=evaluation.all_required_met,
            sufficient=sufficient,
        )
