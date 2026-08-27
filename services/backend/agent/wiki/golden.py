"""Golden Dataset + Shadow/Offline Evaluation（Phase 20 / PR-20）。

- `GoldenTask`：固定知识任务的 Ground Truth（§23），覆盖明确能力、限制、
  Unknown、Ambiguous/Conflict、Multi-hop 等。
- `evaluate_task(bundle, task)`：按 Ground Truth 计算单任务通过/失败与证据指标
  （Grounding Precision / Recall、Forbidden Claim Rate、Status 匹配）。
- `summarize_eval(results)`：聚合为 Production Gate 指标
  （Grounding Rate、Unsupported Claim Rate、Status Accuracy 等）。

设计：纯确定性、不依赖 LLM/网络，可直接离线 `run_golden(provider, tasks)` 评估。
Production Gate 门槛见计划 §23（grounding 100%、unsupported ≤0.5% 等）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from agent.wiki.evidence import EvidenceBundle
from pydantic import BaseModel, Field


class GoldenTask(BaseModel):
    """一个 Golden 任务的 Ground Truth。"""

    task_id: str = Field(description="唯一任务 ID")
    query: str = Field(description="知识查询")
    task_type: str = Field(default="score", description="score/draft/chat")
    product_ids: list[str] = Field(default_factory=list)
    expected_entity: list[str] = Field(default_factory=list, description="应定位的产品/实体")
    required_claims: list[str] = Field(default_factory=list, description="必须出现的事实")
    forbidden_claims: list[str] = Field(default_factory=list, description="禁止出现的事实")
    expected_status: str = Field(
        default="SUFFICIENT",
        description="期望 Bundle status：SUFFICIENT/INSUFFICIENT_EVIDENCE/CONFLICTED",
    )
    expected_source_ids: list[str] = Field(default_factory=list, description="期望命中的 Source id")


# ── 小型代表性 Golden 集（示例/离线用，可按需扩充到 100–300 条）──────────
GOLDEN_DATASET: list[GoldenTask] = [
    GoldenTask(
        task_id="golden-001",
        query="支持哪些智能体身份认证协议？",
        product_ids=["aiscm"],
        expected_entity=["aiscm"],
        required_claims=["OIDC", "联合身份"],
        expected_status="SUFFICIENT",
    ),
    GoldenTask(
        task_id="golden-002",
        query="该产品是否支持短信验证码作为唯一认证因子？",
        product_ids=["aiscm"],
        forbidden_claims=["短信验证码是唯一认证因子"],
        required_claims=[],
        expected_status="INSUFFICIENT_EVIDENCE",
    ),
    GoldenTask(
        task_id="golden-003",
        query="未知产品：S7 WOK 永恒机甲的身份防护能力",
        product_ids=["s7wok"],
        expected_status="INSUFFICIENT_EVIDENCE",
    ),
    GoldenTask(
        task_id="golden-004",
        query="同时适配智能体与人类账号的会话管理",
        product_ids=["aiscm"],
        expected_entity=["aiscm"],
        required_claims=["会话管理"],
        expected_status="SUFFICIENT",
    ),
]


@dataclass
class EvalResult:
    """单任务评估结果。"""

    task: GoldenTask
    bundle: EvidenceBundle
    status_match: bool
    covered_claims: list[str] = field(default_factory=list)
    required_claims: list[str] = field(default_factory=list)
    violated_forbidden_claims: list[str] = field(default_factory=list)
    missing_entities: list[str] = field(default_factory=list)
    grounding_precision: float = 1.0
    unsupported_claim_rate: float = 0.0
    latency_ms: float = 0.0

    @property
    def passed(self) -> bool:
        return (
            self.status_match and not self.missing_entities and not self.violated_forbidden_claims
        )


def evaluate_task(
    bundle: EvidenceBundle, task: GoldenTask, *, latency_ms: float = 0.0
) -> EvalResult:
    """按 Ground Truth 评估一个 bundle。纯函数、确定性。"""
    facts = [e.fact for e in bundle.verified()]
    raw_facts = [e.fact for e in bundle.evidence]

    # Status 命中（Unknown/Ambiguous → INSUFFICIENT_EVIDENCE / CONFLICTED）
    status_match = (bundle.status or "") == task.expected_status

    covered = [c for c in task.required_claims if _contains_any(facts, c)]
    missing = [c for c in task.required_claims if c not in covered]
    violated = [c for c in task.forbidden_claims if _contains_any(raw_facts, c)]
    missing_entities = [e for e in task.expected_entity if e not in bundle.product_ids]

    # Grounding：verified() 中已 grounding 的比例
    verified = bundle.verified()
    unsupported = [e for e in bundle.evidence if e not in verified and e.confidence > 0]
    precision = (len(verified) / len(bundle.evidence)) if bundle.evidence else 1.0
    unsupported_rate = len(unsupported) / len(bundle.evidence) if bundle.evidence else 0.0

    return EvalResult(
        task=task,
        bundle=bundle,
        status_match=status_match,
        covered_claims=covered,
        required_claims=missing,
        violated_forbidden_claims=violated,
        missing_entities=missing_entities,
        grounding_precision=round(precision, 4),
        unsupported_claim_rate=round(unsupported_rate, 4),
        latency_ms=latency_ms,
    )


def summarize_eval(results: Sequence[EvalResult]) -> dict[str, Any]:
    """聚合为 Production Gate 指标（§23）。"""
    if not results:
        return {
            "count": 0,
            "pass_rate": 0.0,
            "grounding_rate": 0.0,
            "unsupported_claim_rate": 0.0,
            "status_accuracy": 0.0,
            "avg_latency_ms": 0.0,
        }
    n = len(results)
    return {
        "count": n,
        "pass_rate": round(sum(1 for r in results if r.passed) / n, 4),
        "grounding_rate": round(sum(r.grounding_precision for r in results) / n, 4),
        "unsupported_claim_rate": round(sum(r.unsupported_claim_rate for r in results) / n, 4),
        "status_accuracy": round(sum(1 for r in results if r.status_match) / n, 4),
        "avg_latency_ms": round(sum(r.latency_ms for r in results) / n, 3),
    }


async def run_golden(provider: Any, tasks: Sequence[GoldenTask] | None = None) -> list[EvalResult]:
    """离线评估：对每个 golden 任务跑一次 provider.collect_evidence 并打分。

    provider 需实现 `async collect_evidence(request) -> EvidenceBundle`。
    """
    import time

    from agent.wiki.provider import KnowledgeRequest

    tasks = list(tasks or GOLDEN_DATASET)
    results: list[EvalResult] = []
    for task in tasks:
        t0 = time.perf_counter()
        bundle = await provider.collect_evidence(
            KnowledgeRequest(
                query=task.query,
                product_ids=list(task.product_ids) or None,
                task_type=task.task_type,
            )
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        results.append(evaluate_task(bundle, task, latency_ms=latency_ms))
    return results


def _contains_any(facts: list[str], claim: str) -> bool:
    """claim 是否与任一 fact 有关键字命中（配合计划"超过=Allowed Values"，用于 Golden 匹配）。"""
    import re

    if not claim:
        return True
    tokens = [t for t in re.split(r"[\s，。,；；+&]+", claim) if t]
    if not tokens:
        return False
    for fact in facts:
        norm = fact.lower()
        if all(t.lower() in norm for t in tokens):
            return True
    return False
