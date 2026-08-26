"""Wiki Telemetry - Runtime 取证与可观测性（PR-12 延伸）。

职责：
  - 记录每次 EvidenceBundle 收集：evidence 数、覆盖度、置信度、状态
  - 统计 grounding 率（verified evidence / candidates）
  - 记录 shadow 模式下 legacy vs wiki 的对比
  - 提供快照供 `/metrics` / trace 消费

设计约束：
  - 进程内、确定性、可选持久化
  - 业务 Agent 只注入 telemetry，不影响 EvidenceBundle 语义
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent.wiki.evidence import EvidenceBundle

logger = logging.getLogger("backend.agent.wiki.telemetry")

GRACE_THRESHOLD = 0.8


@dataclass
class _RunStat:
    """单次收集的统计。"""

    mode: str
    evidence_count: int = 0
    grounded_count: int = 0
    coverage: float = 0.0
    confidence: float = 0.0
    status: str = ""
    wiki_version: str = ""
    visited_count: int = 0
    conflict_count: int = 0


@dataclass
class _ShadowComparison:
    """shadow 模式单次 legacy vs wiki 对比。"""

    wiki_version: str
    legacy_evidence: int
    wiki_evidence: int
    agreements: list[str] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)


class WikiTelemetry:
    """进程内 Wiki 运行时遥测。"""

    def __init__(self):
        self._runs: list[_RunStat] = []
        self._comparisons: list[_ShadowComparison] = []

    # ── 记录 ──────────────────────────────────────────────

    def record_bundle(self, bundle: EvidenceBundle, mode: str = "wiki") -> None:
        grounded = len(bundle.verified(GRACE_THRESHOLD))
        self._runs.append(
            _RunStat(
                mode=mode,
                evidence_count=len(bundle.evidence),
                grounded_count=grounded,
                coverage=bundle.coverage,
                confidence=bundle.confidence,
                status=bundle.status,
                wiki_version=bundle.wiki_version,
                visited_count=len(bundle.visited_pages),
                conflict_count=len(bundle.conflicts),
            )
        )
        logger.info(
            "telemetry record mode=%s status=%s evidence=%d grounded=%d",
            mode,
            bundle.status,
            len(bundle.evidence),
            grounded,
        )

    def record_shadow(self, comparison: dict[str, Any]) -> None:
        """记录 shadow 对比数据（推荐用 ShadowKnowledgeProvider.last_comparison 填充）。"""
        self._comparisons.append(
            _ShadowComparison(
                wiki_version=str(comparison.get("wiki_version", "")),
                legacy_evidence=int(comparison.get("legacy_evidence", 0)),
                wiki_evidence=int(comparison.get("wiki_evidence", 0)),
                agreements=list(comparison.get("agreements", [])),
                divergences=list(comparison.get("divergences", [])),
            )
        )

    # ── 快照 ──────────────────────────────────────────────

    def snapshot(self) -> dict:
        """聚合遥测快照。"""
        runs = len(self._runs)
        shadow_count = len(self._comparisons)
        if runs == 0:
            return {
                "runs": 0,
                "mode_distribution": {},
                "avg_grounding_rate": 0.0,
                "avg_coverage": 0.0,
                "avg_confidence": 0.0,
                "status_distribution": {},
                "conflict_rate": 0.0,
                "shadow_comparisons": shadow_count,
            }
        mode_buckets: dict[str, int] = {}
        status_buckets: dict[str, int] = {}
        grounding_rates: list[float] = []
        coverages: list[float] = []
        confidences: list[float] = []
        conflicts = 0
        for r in self._runs:
            mode_buckets[r.mode] = mode_buckets.get(r.mode, 0) + 1
            status_buckets[r.status] = status_buckets.get(r.status, 0) + 1
            grounding_rates.append(r.grounded_count / r.evidence_count if r.evidence_count else 0.0)
            coverages.append(r.coverage)
            confidences.append(r.confidence)
            conflicts += r.conflict_count
        return {
            "runs": runs,
            "mode_distribution": mode_buckets,
            "avg_grounding_rate": round(sum(grounding_rates) / runs, 4),
            "avg_coverage": round(sum(coverages) / runs, 4),
            "avg_confidence": round(sum(confidences) / runs, 4),
            "status_distribution": status_buckets,
            "conflict_rate": round(conflicts / runs, 4),
            "shadow_comparisons": shadow_count,
        }

    def reset(self) -> None:
        self._runs.clear()
        self._comparisons.clear()
