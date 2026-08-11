"""统一追溯 — 阶段3 WBS 3.2（trace API 的数据层）。

一个 run 的追溯页面必须回答 7 个问题（阶段3 §1.1）：
  1. 为什么选择当前执行模式和模型？
  2. 使用了哪些 prompt、skill、知识和上下文版本？
  3. 每一步计划了什么、执行了什么、为何允许或拒绝？
  4. 使用了哪些证据，哪些验收条件被覆盖？
  5. 消耗了多少 Token、费用和时间？
  6. 发生了什么错误，系统采用了哪种策略？
  7. 为什么完成、部分完成、等待审批或失败？

实现原则：
  - 保留物理集合（agent_run_events / runtime_events / runtime_runs / runtime_manifests），
    本模块提供统一查询层与稳定 schema（TraceReport）；
  - SSE 只是展示渠道，Mongo/事件存储才是事实来源；
  - 不保存密钥、完整正文、完整工具参数或私有思维链（沿用各存储的脱敏字段）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.run_manifest import ExecutionMode, RunManifest, manifest_fingerprint
from agent.runtime_state import EvidenceRecord, RuntimeState, RuntimeStatus
from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

# 追溯页 7 个标准问题（稳定顺序，供 UI/CLI 消费）
TRACE_QUESTIONS = (
    "mode_and_model",  # 1. 为什么选择当前执行模式和模型
    "version_manifest",  # 2. 使用了哪些 prompt/skill/知识/上下文版本
    "step_decisions",  # 3. 每一步计划了什么、执行了什么、为何允许或拒绝
    "evidence_coverage",  # 4. 使用了哪些证据，哪些验收条件被覆盖
    "token_cost_time",  # 5. 消耗了多少 Token、费用和时间
    "errors_and_recovery",  # 6. 发生了什么错误，系统采用了哪种策略
    "final_outcome",  # 7. 为什么完成、部分完成、等待审批或失败
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class StepTrace(BaseModel):
    """单步追溯（decision_summary + 对应证据/工具记录）。"""

    step_id: str = ""
    phase: str = ""
    action: str = ""
    tool_name: str = ""
    outcome: str = ""
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class TraceReport(BaseModel):
    """统一追溯报告：合并清单 + 状态 + 事件，回答 7 个问题。"""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    trace_id: str = ""
    thread_id: str = ""
    user_id: str = ""
    tenant_id: str = ""

    status: str = ""
    reason_code: str = ""
    reason: str = ""

    # Q1：模式与模型
    execution_mode: str = ""
    model_provider: str = ""
    model_id: str = ""
    model_revision: str = ""
    feature_flags: dict[str, Any] = Field(default_factory=dict)
    code_revision: str = ""

    # Q2：版本清单
    prompt_refs: list[dict[str, str]] = Field(default_factory=list)
    skill_snapshot_hash: str = ""
    knowledge_snapshot_hash: str = ""
    context_plan_hash: str = ""
    tool_registry_version: str = ""
    pricing_version: str = ""
    manifest_fingerprint: str = ""
    manifest_saved: bool = False

    # Q3：逐步决策
    steps: list[StepTrace] = Field(default_factory=list)
    allowed_count: int = 0
    denied_count: int = 0

    # Q4：证据覆盖
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    covered_acceptance: list[int] = Field(default_factory=list)
    evidence_coverage_ratio: float = 0.0

    # Q5：Token/费用/时间
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    steps_run: int = 0
    duration_seconds: float = 0.0

    # Q6：错误与恢复策略
    error_events: list[dict[str, Any]] = Field(default_factory=list)
    recovery_decisions: list[str] = Field(default_factory=list)

    # Q7：终态原因
    outcome_summary: str = ""

    @property
    def answers(self) -> dict[str, str]:
        """7 问的自然语言回答（供追溯页面/CLI 直接展示）。"""
        q1 = f"模式={self.execution_mode} 模型={self.model_provider}/{self.model_id}"
        if self.model_revision:
            q1 += f"@{self.model_revision}"
        q1 += f" 代码={self.code_revision or '(未记录)'}"
        q2 = (
            f"prompts={len(self.prompt_refs)} 条 skill={self.skill_snapshot_hash or '-'} "
            f"knowledge={self.knowledge_snapshot_hash or '-'} "
            f"context={self.context_plan_hash or '-'} tools=v{self.tool_registry_version or '-'}"
        )
        q3 = (
            f"{len(self.steps)} 步（允许 {self.allowed_count} / 拒绝 {self.denied_count}），"
            f"末步={self.steps[-1].action if self.steps else '-'}"
        )
        q4 = (
            f"证据 {len(self.evidence)} 条，覆盖验收 {len(self.covered_acceptance)}/"
            f"{max(1, len(self.acceptance_criteria))}（{self.evidence_coverage_ratio:.0%}）"
        )
        q5 = (
            f"token {self.input_tokens}+{self.output_tokens}={self.total_tokens} "
            f"cost=${self.cost_usd:.6f} 步骤={self.steps_run} "
            f"耗时={self.duration_seconds:.1f}s"
        )
        q6 = (
            f"错误 {len(self.error_events)} 次，恢复策略 {len(self.recovery_decisions)} 次"
            if self.error_events
            else "无错误"
        )
        q7 = self.outcome_summary or f"终态 {self.status}"
        return {
            "mode_and_model": q1,
            "version_manifest": q2,
            "step_decisions": q3,
            "evidence_coverage": q4,
            "token_cost_time": q5,
            "errors_and_recovery": q6,
            "final_outcome": q7,
        }


def build_trace(
    *,
    state: RuntimeState,
    manifest: RunManifest | None = None,
    runtime_events: list[Any] | None = None,
    agent_events: list[Any] | None = None,
    created_at: datetime | None = None,
) -> TraceReport:
    """从已加载的 state / manifest / 事件聚合追溯报告（纯函数，便于测试）。"""
    stamp = created_at or _utc_now()

    steps = [
        StepTrace(
            step_id=d.step_id,
            phase=d.phase,
            action=d.action,
            tool_name=d.tool_name,
            outcome=d.outcome,
            reason=d.reason,
            evidence_ids=[e.evidence_id for e in _evidence_for_step(state, d.step_id)],
            created_at=d.created_at,
        )
        for d in state.decision_summaries
    ]

    covered = sorted({e.acceptance_index for e in state.evidence if e.acceptance_index is not None})
    criteria_n = len(state.acceptance_criteria) or 1

    runtime_events = runtime_events or []
    error_events = [
        _event_payload(e)
        for e in runtime_events
        if getattr(e, "event_type", "") in ("step_failed", "run_failed", "retrying")
        or (getattr(e, "payload", {}) or {}).get("recovery_action")
    ]
    recovery_decisions = [
        str((getattr(e, "payload", {}) or {}).get("recovery_action"))
        for e in runtime_events
        if (getattr(e, "payload", {}) or {}).get("recovery_action")
    ]

    outcome = _outcome_summary(state)
    return TraceReport(
        run_id=state.run_id,
        trace_id=state.trace_id,
        thread_id=state.thread_id,
        user_id=state.user_id,
        tenant_id=state.tenant_id,
        status=state.status.value,
        reason_code=getattr(state, "reason_code", "") or "",
        reason=getattr(state, "reason", "") or "",
        # 无冻结清单时仍给出可读模式（默认 autonomous，与 RunManifest 默认一致）
        execution_mode=manifest.execution_mode.value if manifest else ExecutionMode.AUTONOMOUS.value,
        model_provider=manifest.model_provider if manifest else "",
        model_id=manifest.model_id if manifest else "",
        model_revision=manifest.model_revision if manifest else "",
        feature_flags=dict(manifest.feature_flags) if manifest else {},
        code_revision=manifest.code_revision if manifest else "",
        prompt_refs=list(manifest.prompt_refs) if manifest else [],
        skill_snapshot_hash=manifest.skill_snapshot_hash if manifest else "",
        knowledge_snapshot_hash=manifest.knowledge_snapshot_hash if manifest else "",
        context_plan_hash=manifest.context_plan_hash if manifest else "",
        tool_registry_version=manifest.tool_registry_version if manifest else "",
        pricing_version=manifest.pricing_version if manifest else "",
        manifest_fingerprint=manifest_fingerprint(manifest) if manifest else "",
        manifest_saved=manifest is not None,
        steps=steps,
        allowed_count=sum(1 for d in state.decision_summaries if d.outcome in ("success", "approved")),
        denied_count=sum(1 for d in state.decision_summaries if d.outcome in ("failed", "denied", "skipped")),
        evidence=list(state.evidence),
        acceptance_criteria=list(state.acceptance_criteria),
        covered_acceptance=covered,
        evidence_coverage_ratio=round(len(covered) / criteria_n, 4),
        input_tokens=state.usage.input_tokens,
        output_tokens=state.usage.output_tokens,
        total_tokens=state.usage.total_tokens,
        cost_usd=round(state.usage.cost_usd, 6),
        steps_run=state.usage.steps,
        duration_seconds=_duration(state, stamp),
        error_events=error_events,
        recovery_decisions=recovery_decisions,
        outcome_summary=outcome,
    )


def _evidence_for_step(state: RuntimeState, step_id: str) -> list[EvidenceRecord]:
    return [e for e in state.evidence if e.step_id == step_id]


def _event_payload(event: Any) -> dict[str, Any]:
    payload = dict(getattr(event, "payload", {}) or {})
    payload.setdefault("event_type", getattr(event, "event_type", ""))
    payload.setdefault("sequence", getattr(event, "sequence", 0))
    return payload


def _duration(state: RuntimeState, now: datetime) -> float:
    start = state.usage.started_at
    end = state.updated_at or now
    try:
        return max(0.0, (end - start).total_seconds())
    except TypeError:
        return 0.0


def _outcome_summary(state: RuntimeState) -> str:
    status = state.status
    if status in (RuntimeStatus.COMPLETED,):
        return f"已完成：{len(state.completed_steps)} 步成功，满足验收 {len(state.evidence)} 条证据"
    if status == RuntimeStatus.WAITING_APPROVAL:
        pending = len(state.approval_state.pending_approvals)
        return f"等待审批：{pending} 项待人工审批"
    if status == RuntimeStatus.CANCELED:
        return "已取消（用户在安全点取消）"
    if status == RuntimeStatus.BUDGET_EXCEEDED:
        return "预算耗尽：Token/费用/时间超出上限，降级终态"
    if status == RuntimeStatus.STOPPED:
        return "策略停止：熔断/租约丢失/系统关闭"
    if status == RuntimeStatus.FAILED:
        return f"失败：{getattr(state, 'reason_code', '') or '未分类异常'}（{len(state.failed_steps)} 步失败）"
    if status == RuntimeStatus.CANCEL_REQUESTED:
        return "取消请求已提交，等待安全点停止"
    return f"进行中：{status.value}（已完成 {len(state.completed_steps)} 步）"


async def load_trace(
    *,
    manifest_store,
    state_store,
    run_id: str,
    runtime_event_store: Any | None = None,
    agent_event_store: Any | None = None,
) -> TraceReport | None:
    """统一查询层：从各存储加载并聚合追溯报告（trace API 数据源）。"""
    state = await state_store.load(run_id)
    if state is None:
        return None
    manifest = await manifest_store.load(run_id)
    events = []
    if runtime_event_store is not None:
        events.extend(await runtime_event_store.list_run_events(run_id))
    if agent_event_store is not None:
        events.extend(await agent_event_store.list_run_events(run_id))
    return build_trace(state=state, manifest=manifest, runtime_events=events)
