"""RuntimeState 与运行级预算 — 阶段四 4A Step 4A-1 / 4A-2。

设计约束：
  - 显式 schema 版本 + 单一迁移入口 migrate_runtime_state；
  - 终态不可由普通步骤重新变成运行态；
  - checkpoint_version 递增 + apply_state_mutation 版本检查，拒绝旧执行器覆盖新状态；
  - 每次状态迁移生成脱敏审计事件（不含 goal 全文、密钥、模型私有推理）；
  - 状态中不保存提示词全文、密钥或私有思维链。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"


# ═══════════════════════════════════════════════════════════════
# 状态
# ═══════════════════════════════════════════════════════════════


class RuntimeStatus(str, Enum):
    """自主运行状态。终态不可逆。"""

    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    BUDGET_EXCEEDED = "budget_exceeded"
    STOPPED = "stopped"  # 策略熔断 / 租约丢失 / 系统关闭


# 终态：到达后任何 transition 都会被拒绝
TERMINAL_STATUSES = frozenset(
    {
        RuntimeStatus.COMPLETED,
        RuntimeStatus.FAILED,
        RuntimeStatus.CANCELED,
        RuntimeStatus.BUDGET_EXCEEDED,
        RuntimeStatus.STOPPED,
    }
)


class RuntimeStateError(Exception):
    """状态非法转换或版本冲突。"""


class RuntimeStateTransitionError(RuntimeStateError):
    """终态不可逆 / 非法状态转换。"""


class RuntimeStateConflictError(RuntimeStateError):
    """并发版本冲突：旧执行器覆盖新状态被拒绝。"""


# ═══════════════════════════════════════════════════════════════
# 统一预算模型（运行级）
# ═══════════════════════════════════════════════════════════════


class RunBudget(BaseModel):
    """运行级预算上限（不可变，启动时从 Settings 冻结）。"""

    model_config = ConfigDict(frozen=True)

    max_steps: int = 20
    max_runtime_seconds: int = 600
    max_input_tokens: int = 24000
    max_output_tokens: int = 4000
    max_total_tokens: int = 0  # 0 = 由单项上限兜底
    max_tool_calls: int = 40
    max_parallel_tools: int = 3
    max_tool_concurrency: int = 3
    max_cost_usd: float = 0.0  # 0 = 不限制
    max_retries: int = 2  # 单步最大重试次数（不含首次）
    max_consecutive_failures: int = 3
    remote_agent_quota: int = 5  # 单个远端 Agent 调用配额（A2A 预留）
    allowed_tool_names: frozenset[str] = Field(default_factory=frozenset)
    deadline_at: datetime | None = None  # 等价于 max_runtime_seconds 的绝对截止

    @classmethod
    def from_settings(cls, settings) -> RunBudget:
        """从 Settings 冻结运行级预算（配置非法时由 Settings 校验提前拒绝）。"""
        return cls(
            max_steps=settings.AUTONOMOUS_MAX_STEPS,
            max_runtime_seconds=settings.AUTONOMOUS_MAX_RUNTIME_SECONDS,
            max_input_tokens=settings.AUTONOMOUS_MAX_INPUT_TOKENS,
            max_output_tokens=settings.AUTONOMOUS_MAX_OUTPUT_TOKENS,
            max_total_tokens=settings.AUTONOMOUS_MAX_TOTAL_TOKENS,
            max_tool_calls=settings.AUTONOMOUS_MAX_TOOL_CALLS,
            max_parallel_tools=settings.AUTONOMOUS_MAX_PARALLEL_TOOLS,
            max_tool_concurrency=settings.AUTONOMOUS_MAX_TOOL_CONCURRENCY,
            max_cost_usd=settings.AUTONOMOUS_MAX_COST_USD,
            max_retries=settings.AUTONOMOUS_MAX_RETRIES,
            max_consecutive_failures=settings.AUTONOMOUS_MAX_CONSECUTIVE_FAILURES,
        )


class BudgetUsage(BaseModel):
    """运行预算用量（可变，运行中累加）。"""

    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    retries: int = 0
    consecutive_failures: int = 0
    remote_agent_calls: dict[str, int] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_action_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def elapsed_seconds(self, *, now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        return max(0.0, (current - self.started_at).total_seconds())

    def exceeded(self, budget: RunBudget, *, now: datetime | None = None) -> list[str]:
        """返回被触发的硬上限列表（空 = 未超限）。"""
        broken: list[str] = []
        if self.steps >= budget.max_steps:
            broken.append("max_steps")
        if self.input_tokens >= budget.max_input_tokens:
            broken.append("max_input_tokens")
        if self.output_tokens >= budget.max_output_tokens:
            broken.append("max_output_tokens")
        if budget.max_total_tokens > 0 and self.total_tokens >= budget.max_total_tokens:
            broken.append("max_total_tokens")
        if self.tool_calls >= budget.max_tool_calls:
            broken.append("max_tool_calls")
        if budget.max_cost_usd > 0 and self.cost_usd >= budget.max_cost_usd:
            broken.append("max_cost_usd")
        if self.consecutive_failures >= budget.max_consecutive_failures:
            broken.append("max_consecutive_failures")
        current = now or datetime.now(UTC)
        if budget.deadline_at is not None and current >= budget.deadline_at:
            broken.append("deadline")
        elif self.elapsed_seconds(now=current) >= budget.max_runtime_seconds:
            broken.append("max_runtime_seconds")
        return broken

    def can_continue(self, budget: RunBudget, *, now: datetime | None = None) -> bool:
        """是否还有预算继续运行（宽松检查：容忍刚好到达上限）。"""
        return not self.exceeded(budget, now=now)

    def can_start_next_action(self, budget: RunBudget, *, now: datetime | None = None) -> bool:
        """预算检查点：规划前 / 模型调用前 / 工具调用前 / 重试前 / 进入下一步骤前。

        任一硬上限触发后不得继续启动新动作（比 can_continue 更严格）。
        """
        if self.exceeded(budget, now=now):
            return False
        # 下一步骤 / 重试还会占用配额，提前一步预留
        if self.steps + 1 > budget.max_steps:
            return False
        if self.tool_calls + 1 > budget.max_tool_calls:
            return False
        return True

    def record_step(self, *, tokens_in: int = 0, tokens_out: int = 0,
                    cost: float = 0.0, now: datetime | None = None) -> BudgetUsage:
        self.steps += 1
        return self.record_tokens(tokens_in=tokens_in, tokens_out=tokens_out, cost=cost, now=now)

    def record_tokens(self, *, tokens_in: int = 0, tokens_out: int = 0,
                      cost: float = 0.0, now: datetime | None = None) -> BudgetUsage:
        self.input_tokens += max(0, tokens_in)
        self.output_tokens += max(0, tokens_out)
        self.cost_usd += max(0.0, cost)
        self.last_action_at = now or datetime.now(UTC)
        return self

    def record_tool_call(self, now: datetime | None = None) -> BudgetUsage:
        self.tool_calls += 1
        self.last_action_at = now or datetime.now(UTC)
        return self

    def record_retry(self, now: datetime | None = None) -> BudgetUsage:
        self.retries += 1
        self.last_action_at = now or datetime.now(UTC)
        return self

    def record_success(self, now: datetime | None = None) -> BudgetUsage:
        self.consecutive_failures = 0
        self.last_action_at = now or datetime.now(UTC)
        return self

    def record_failure(self, now: datetime | None = None) -> BudgetUsage:
        self.consecutive_failures += 1
        self.last_action_at = now or datetime.now(UTC)
        return self

    def record_remote_agent_call(self, agent_id: str, now: datetime | None = None) -> BudgetUsage:
        self.remote_agent_calls[agent_id] = self.remote_agent_calls.get(agent_id, 0) + 1
        self.last_action_at = now or datetime.now(UTC)
        return self


# ═══════════════════════════════════════════════════════════════
# 记录
# ═══════════════════════════════════════════════════════════════


def _stable_hash(payload: dict[str, Any]) -> str:
    import json

    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ToolResultRecord(BaseModel):
    """工具调用记录（脱敏：只存摘要哈希，不存参数/结果原文）。"""

    tool_id: str
    display_name: str = ""
    ok: bool = True
    error_code: str = ""
    args_hash: str = ""
    result_hash: str = ""
    duration_ms: int = 0
    idempotency_key: str = ""
    source_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvidenceRecord(BaseModel):
    """验收条件证据。"""

    evidence_id: str
    step_id: str = ""
    acceptance_index: int | None = None  # 对应 acceptance_criteria 的下标
    kind: str = "artifact"  # artifact / tool_result / approval / manual
    ref: str = ""  # 引用（产物 ID / 工具结果 ID / 审批 ID）
    hash: str = ""
    note: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DecisionSummary(BaseModel):
    """决策摘要（可审计；不包含模型私有推理链）。"""

    step_id: str = ""
    phase: str = "plan"  # plan / policy / execute / validate
    action: str = ""
    tool_name: str = ""
    args_hash: str = ""
    result_hash: str = ""
    outcome: str = ""  # success / failed / skipped / approved / denied
    reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PendingApproval(BaseModel):
    """待人工审批项。"""

    approval_id: str
    action: str = ""
    risk_level: str = "L2"  # L0/L1/L2/L3
    params_hash: str = ""
    params_summary: str = ""
    trigger_rule: str = ""
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"
    approver: str = ""
    expires_at: datetime | None = None
    one_time_token: str = ""
    decision_summary_id: str = ""


class ApprovalState(BaseModel):
    """审批状态汇总。"""

    pending_approvals: list[PendingApproval] = Field(default_factory=list)
    approved_tokens: list[str] = Field(default_factory=list)  # 一次性授权标识
    consumed_tokens: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# RuntimeState
# ═══════════════════════════════════════════════════════════════


class RuntimeState(BaseModel):
    """可序列化、可迁移的自主运行状态。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = SCHEMA_VERSION
    run_id: str
    thread_id: str = ""
    trace_id: str = ""
    user_id: str = ""
    tenant_id: str = ""

    goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)

    status: RuntimeStatus = RuntimeStatus.PENDING
    current_step: str = ""
    plan_version: int = 0

    completed_steps: list[str] = Field(default_factory=list)
    pending_steps: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)

    tool_results: list[ToolResultRecord] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    decision_summaries: list[DecisionSummary] = Field(default_factory=list)

    budget: RunBudget = Field(default_factory=RunBudget)
    usage: BudgetUsage = Field(default_factory=BudgetUsage)

    approval_state: ApprovalState = Field(default_factory=ApprovalState)

    checkpoint_version: int = 1
    fencing_token: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # ── 状态机 ──────────────────────────────────────────

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def transition_to(
        self,
        next_status: RuntimeStatus,
        *,
        reason: str = "",
        actor: str = "runtime",
        now: datetime | None = None,
        reason_code: str = "",
    ) -> RuntimeState:
        """状态迁移：终态不可逆；每次迁移生成脱敏审计事件并递增版本。

        Returns:
            (state, audit_event) —— 新状态与审计事件。
        """
        current = self.status
        if not isinstance(next_status, RuntimeStatus):
            next_status = RuntimeStatus(next_status)
        if current in TERMINAL_STATUSES:
            raise RuntimeStateTransitionError(
                f"terminal status {current.value} cannot transition to {next_status.value}"
            )
        stamp = now or datetime.now(UTC)
        new_state = self.model_copy(
            update={
                "status": next_status,
                "updated_at": stamp,
                "checkpoint_version": self.checkpoint_version + 1,
            }
        )
        audit = self._audit_event(current, next_status, reason=reason, actor=actor,
                                  reason_code=reason_code, stamp=stamp)
        return new_state, audit

    def _audit_event(
        self,
        from_status: RuntimeStatus,
        to_status: RuntimeStatus,
        *,
        reason: str,
        actor: str,
        reason_code: str,
        stamp: datetime,
    ) -> dict[str, Any]:
        """脱敏审计事件：不含 goal 全文、参数、密钥或私有推理。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": _stable_hash(
                {"run_id": self.run_id, "sequence": self.checkpoint_version, "to": to_status.value}
            ),
            "sequence": self.checkpoint_version,
            "run_id": self.run_id,
            "timestamp": stamp.isoformat(),
            "event_type": "state_transition",
            "from_status": from_status.value,
            "to_status": to_status.value,
            "reason": reason[:200],
            "reason_code": reason_code,
            "actor": actor,
            "checkpoint_version": self.checkpoint_version + 1,
            "fencing_token": self.fencing_token,
        }


# ═══════════════════════════════════════════════════════════════
# 迁移与并发
# ═══════════════════════════════════════════════════════════════


def migrate_runtime_state(raw: dict[str, Any]) -> RuntimeState:
    """单一迁移入口：将任意历史版本状态转为当前版本 RuntimeState。"""
    version = raw.get("schema_version")
    if version == SCHEMA_VERSION:
        return RuntimeState.model_validate(raw)
    if version in (None, "0", "0.1"):
        # 历史版本：缺失字段取默认值；新增字段为空列表/默认预算
        migrated: dict[str, Any] = dict(raw)
        migrated["schema_version"] = SCHEMA_VERSION
        return RuntimeState.model_validate(migrated)
    raise ValueError(f"unsupported runtime_state schema_version: {version!r}")


def apply_state_mutation(
    base: RuntimeState,
    *,
    expected_version: int,
    mutation: Callable[[RuntimeState], RuntimeState],
) -> RuntimeState:
    """版本检查后的状态更新：expected_version 不匹配则拒绝（旧执行器覆盖新状态）。

    每次成功更新递增 checkpoint_version，供持久化层做乐观锁 CAS。
    """
    if base.checkpoint_version != expected_version:
        raise RuntimeStateConflictError(
            f"state conflict: expected checkpoint_version={expected_version}, "
            f"actual={base.checkpoint_version}"
        )
    mutated = mutation(base)
    return mutated.model_copy(
        update={"checkpoint_version": base.checkpoint_version + 1}
    )
