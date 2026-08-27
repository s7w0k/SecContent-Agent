"""ShadowComparator - Legacy primary 与 Skill shadow 的差异评估（§40-43）。

比较维度：task status / artifact types / latency / 可选 output 字段。
生成类产物不做逐字比较（§43）；证据 grounded 在此层由 skill 侧提供，默认从 output 推断。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.execution.contracts import ExecutionRequest, ExecutionResult
from pydantic import BaseModel, Field


class ShadowEvaluation(BaseModel):
    """单次 Shadow 双跑的评估记录（§41）。"""

    task_id: str
    trace_id: str = ""

    legacy_status: str
    skill_status: str

    classification_match: bool | None = None
    product_match: bool | None = None
    score_delta: float | None = None

    artifact_type_match: bool = True

    draft_semantic_similarity: float | None = None

    grounding_valid: bool = True

    legacy_latency_ms: float = 0.0
    skill_latency_ms: float = 0.0

    legacy_cost: float = 0.0
    skill_cost: float = 0.0

    critical_mismatch: bool = False
    mismatch_reasons: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _match(status_l: str, status_s: str) -> bool:
    # SUCCEEDED / PARTIAL 归为一侧，FAILED / BLOCKED 归为另一侧，避免成败不同即 mismatch
    good = {"SUCCEEDED", "PARTIAL"}
    return (status_l in good) == (status_s in good)


class ShadowComparator:
    """比较 Legacy primary 与 Skill shadow，产出 ShadowEvaluation。"""

    async def compare(
        self,
        *,
        request: ExecutionRequest,
        primary: ExecutionResult,
        shadow: ExecutionResult | None,
    ) -> ShadowEvaluation:
        reasons: list[str] = []
        critical_mismatch = False

        if shadow is None:
            return ShadowEvaluation(
                task_id=request.task_id,
                trace_id=request.trace_id,
                legacy_status=primary.status,
                skill_status="NOT_RUN",
                critical_mismatch=False,
                mismatch_reasons=["shadow_did_not_run"],
            )

        if not _match(primary.status, shadow.status):
            reasons.append(f"status_mismatch: legacy={primary.status} skill={shadow.status}")

        # Artifact 类型比较：仅比率（是否产出 artifact）
        shadow_arts = set(shadow.artifact_refs)
        if primary.status == "SUCCEEDED" and not shadow_arts:
            reasons.append("skill_artifact_missing")
        artifact_type_match = not (primary.status == "SUCCEEDED" and not shadow_arts)
        if not artifact_type_match:
            critical_mismatch = True

        # 可选字段比较
        classification_match = _compare_opt(
            primary, shadow, "classification", lambda v: v.get("classification")
        )
        product_match = _compare_opt(primary, shadow, "product", lambda v: v.get("product_id"))
        score_delta = None
        ps = primary.output.get("total_score")
        ss = shadow.output.get("total_score")
        if isinstance(ps, (int, float)) and isinstance(ss, (int, float)):
            score_delta = round(float(ss) - float(ps), 3)

        grounding_valid = bool(shadow.output.get("grounding_valid", True))

        skill_cost = float(shadow.metadata.get("estimated_cost", 0) or 0)
        legacy_cost = float(primary.metadata.get("estimated_cost", 0) or 0)

        evaluation = ShadowEvaluation(
            task_id=request.task_id,
            trace_id=request.trace_id,
            legacy_status=primary.status,
            skill_status=shadow.status,
            classification_match=classification_match,
            product_match=product_match,
            score_delta=score_delta,
            artifact_type_match=artifact_type_match,
            grounding_valid=grounding_valid,
            legacy_latency_ms=primary.latency_ms,
            skill_latency_ms=shadow.latency_ms,
            legacy_cost=legacy_cost,
            skill_cost=skill_cost,
            critical_mismatch=critical_mismatch,
            mismatch_reasons=reasons,
        )
        return evaluation


def _compare_opt(
    primary: ExecutionResult,
    shadow: ExecutionResult,
    key: str,
    getter: Any,
) -> bool | None:
    pv = _deep_get(primary.output, key, getter)
    sv = _deep_get(shadow.output, key, getter)
    if pv is None or sv is None:
        return None
    return pv == sv


def _deep_get(output: dict[str, Any], dotted: str, getter: Any) -> Any:
    node: Any = output
    for part in dotted.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    if callable(getter) and isinstance(node, dict):
        try:
            val = getter(node)
            return val if val is not None else None
        except Exception:
            return None
    return node


__all__ = ["ShadowComparator", "ShadowEvaluation"]
