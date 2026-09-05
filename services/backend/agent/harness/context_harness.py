"""Context Harness — 阶段4 §1.3（WBS 4.1）。

RunManifest 驱动上下文构建：
  - 可重复构建验证（相同输入 + 相同来源 → 相同 plan_hash）；
  - legacy/candidate 上下文 diff（新增/删除/截断节）；
  - token 估算 vs 实测偏差统计；
  - 构建审计（清单 hash / 丢弃 / 冲突 / 截断原因可追溯）。

安全约束：diff 与审计只输出节标识、来源版本与 token 数，不输出正文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.context_manager import ContextManager, ContextPlan, ContextRequest, ContextSource

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class ReproducibilityReport:
    """可重复构建验证结果。"""

    plan_hash: str
    stable: bool
    runs: int
    observed_hashes: tuple[str, ...]

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_hash": self.plan_hash,
            "stable": self.stable,
            "runs": self.runs,
            "observed_hashes": list(self.observed_hashes),
        }


@dataclass(frozen=True)
class ContextDiff:
    """legacy/candidate 上下文注入差异。"""

    added: list[str]  # candidate 新增的 source_id
    removed: list[str]  # candidate 移除的 source_id
    truncated: list[str]  # candidate 中被截断的 source_id
    budget_before: int
    budget_after: int
    hash_changed: bool

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "added": self.added,
            "removed": self.removed,
            "truncated": self.truncated,
            "budget_before": self.budget_before,
            "budget_after": self.budget_after,
            "hash_changed": self.hash_changed,
        }


@dataclass(frozen=True)
class TokenDeviationStats:
    """估算 token vs 实测 token 偏差。"""

    estimated_tokens: int
    actual_tokens: int
    deviation_ratio: float  # 实测/估算 - 1（正=低估，负=高估）

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "estimated_tokens": self.estimated_tokens,
            "actual_tokens": self.actual_tokens,
            "deviation_ratio": self.deviation_ratio,
        }


@dataclass(frozen=True)
class ContextAudit:
    """一次上下文构建的审计记录（来源/版本/原因可追溯）。"""

    manifest_hash: str
    plan_hash: str
    total_tokens: int
    budget_tokens: int
    dropped: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    truncated_sections: list[str]

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "manifest_hash": self.manifest_hash,
            "plan_hash": self.plan_hash,
            "total_tokens": self.total_tokens,
            "budget_tokens": self.budget_tokens,
            "dropped": self.dropped,
            "conflicts": self.conflicts,
            "truncated_sections": self.truncated_sections,
        }


class ContextHarness:
    """Manifest 驱动的上下文 Harness。"""

    def __init__(self, *, manager: ContextManager | None = None):
        self.manager = manager or ContextManager()

    # ── 可重复构建 ─────────────────────────────────────────

    def build(self, request: ContextRequest, sources: list[ContextSource]) -> ContextPlan:
        return self.manager.build(request, sources)

    def verify_reproducible(
        self,
        request: ContextRequest,
        sources: list[ContextSource],
        *,
        runs: int = 2,
    ) -> ReproducibilityReport:
        hashes: list[str] = []
        for _ in range(runs):
            hashes.append(self.manager.build(request, sources).plan_hash)
        stable = len(set(hashes)) == 1
        return ReproducibilityReport(
            plan_hash=hashes[0] if hashes else "",
            stable=stable,
            runs=runs,
            observed_hashes=tuple(hashes),
        )

    # ── legacy/candidate diff ──────────────────────────────

    @staticmethod
    def diff_plans(before: ContextPlan, after: ContextPlan) -> ContextDiff:
        def _ids(plan: ContextPlan) -> dict[str, bool]:
            return {s.source_id: s.truncated for s in plan.sections}

        before_ids = _ids(before)
        after_ids = _ids(after)
        added = [sid for sid in after_ids if sid not in before_ids]
        removed = [sid for sid in before_ids if sid not in after_ids]
        truncated = [sid for sid in after_ids if after_ids[sid]]
        return ContextDiff(
            added=sorted(added),
            removed=sorted(removed),
            truncated=sorted(truncated),
            budget_before=before.budget_tokens,
            budget_after=after.budget_tokens,
            hash_changed=before.plan_hash != after.plan_hash,
        )

    # ── token 偏差 ─────────────────────────────────────────

    @staticmethod
    def token_deviation(*, estimated_tokens: int, actual_tokens: int) -> TokenDeviationStats:
        if estimated_tokens <= 0:
            ratio = 1.0 if actual_tokens > 0 else 0.0
        else:
            ratio = (actual_tokens / estimated_tokens) - 1.0
        return TokenDeviationStats(
            estimated_tokens=estimated_tokens,
            actual_tokens=actual_tokens,
            deviation_ratio=round(ratio, 4),
        )

    # ── 审计 ───────────────────────────────────────────────

    @staticmethod
    def audit_plan(plan: ContextPlan, *, manifest_hash: str = "") -> ContextAudit:
        return ContextAudit(
            manifest_hash=manifest_hash,
            plan_hash=plan.plan_hash,
            total_tokens=plan.total_tokens,
            budget_tokens=plan.budget_tokens,
            dropped=[
                {"source": d.source, "reason": d.reason, "tokens": d.tokens} for d in plan.dropped
            ],
            conflicts=[
                {"source": c.source, "suppressed_by": c.suppressed_by} for c in plan.conflicts
            ],
            truncated_sections=sorted(s.source_id for s in plan.sections if s.truncated),
        )
