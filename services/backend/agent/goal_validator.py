"""GoalValidator、循环检测与终止条件 — 阶段四 4A Step 4A-4。

完成判断拆成五层：
  1. 产物结构校验；
  2. 格式、测试或业务规则校验；
  3. 每条验收条件的证据校验（验收条件 → 证据映射）；
  4. 未决审批、失败步骤和预算风险校验；
  5. 高风险目标的人工最终确认。

强制停止条件（decide_termination）：
  验收全过 / 预算上限 / 连续重复（循环） / 连续失败超阈值 /
  无可执行步骤 / 用户取消 / PolicyEngine 熔断 / 租约丢失或系统关闭。

循环检测组合：动作指纹、最近 N 步相似度、相同错误签名、无新增证据重复次数。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from agent.runtime_state import (
    BudgetUsage,
    RuntimeState,
    RuntimeStatus,
    RunBudget,
)


# ═══════════════════════════════════════════════════════════════
# 目标验证
# ═══════════════════════════════════════════════════════════════


class CheckStatus(str, Enum):
    SATISFIED = "satisfied"
    MISSING_EVIDENCE = "missing_evidence"
    NOT_MET = "not_met"


class GoalCheck(BaseModel):
    """单条验收条件检查结果。"""

    index: int
    criterion: str
    status: CheckStatus
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""


class GoalValidationResult(BaseModel):
    """目标完成判断结果。"""

    status: Literal["complete", "incomplete", "evidence_missing", "needs_human_confirm", "blocked"]
    structure_ok: bool = True
    rules_ok: bool = True
    checks: list[GoalCheck] = Field(default_factory=list)
    pending_approvals: int = 0
    failed_steps: int = 0
    budget_risk: list[str] = Field(default_factory=list)
    reason: str = ""
    validated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def satisfied_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.SATISFIED)

    @property
    def total_criteria(self) -> int:
        return len(self.checks)


class GoalValidator:
    """可配置的目标验证器（规则与 schema 由代码构造，模型输出无法修改）。"""

    def __init__(
        self,
        *,
        required_artifact_keys: tuple[str, ...] = ("report",),
        rule_checks: tuple[Callable[[dict[str, Any]], str], ...] = (),
        evidence_required: bool = True,
        high_risk_requires_confirm: bool = True,
    ):
        self.required_artifact_keys = required_artifact_keys
        self.rule_checks = rule_checks
        self.evidence_required = evidence_required
        self.high_risk_requires_confirm = high_risk_requires_confirm

    def validate(
        self,
        state: RuntimeState,
        *,
        artifacts: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> GoalValidationResult:
        artifacts = artifacts or {}
        stamp = now or datetime.now(UTC)

        # 1. 产物结构校验
        missing = [k for k in self.required_artifact_keys if not artifacts.get(k)]
        if missing:
            return GoalValidationResult(
                status="incomplete", structure_ok=False,
                reason=f"missing required artifacts: {missing}", validated_at=stamp,
            )

        # 2. 业务规则校验
        for check in self.rule_checks:
            err = check(artifacts)
            if err:
                return GoalValidationResult(
                    status="incomplete", rules_ok=False,
                    reason=f"rule check failed: {err}", validated_at=stamp,
                )

        # 3. 验收条件证据校验
        checks: list[GoalCheck] = []
        for idx, criterion in enumerate(state.acceptance_criteria):
            matched = [
                e.evidence_id
                for e in state.evidence
                if e.acceptance_index == idx or e.acceptance_index is None
            ]
            if matched:
                checks.append(
                    GoalCheck(index=idx, criterion=criterion, status=CheckStatus.SATISFIED, evidence_ids=matched)
                )
            elif self.evidence_required:
                checks.append(
                    GoalCheck(index=idx, criterion=criterion, status=CheckStatus.MISSING_EVIDENCE,
                              note="no evidence yet")
                )
            else:
                checks.append(GoalCheck(index=idx, criterion=criterion, status=CheckStatus.NOT_MET))

        # 4. 未决审批 / 失败步骤 / 预算风险
        pending = sum(1 for a in state.approval_state.pending_approvals if a.status == "pending")
        failed = len(state.failed_steps)
        budget_risk = state.usage.exceeded(state.budget, now=stamp)

        all_satisfied = checks and all(c.status == CheckStatus.SATISFIED for c in checks)
        if pending:
            return GoalValidationResult(
                status="blocked", checks=checks, pending_approvals=pending,
                failed_steps=failed, budget_risk=budget_risk,
                reason="pending approvals", validated_at=stamp,
            )
        if budget_risk:
            return GoalValidationResult(
                status="blocked", checks=checks, pending_approvals=0,
                failed_steps=failed, budget_risk=budget_risk,
                reason="budget risk", validated_at=stamp,
            )
        if all_satisfied:
            # 5. 高风险目标的人工最终确认
            if self.high_risk_requires_confirm and not state.approval_state.approved_tokens:
                return GoalValidationResult(
                    status="needs_human_confirm", checks=checks,
                    reason="high risk goal requires human final confirmation",
                    validated_at=stamp,
                )
            return GoalValidationResult(
                status="complete", checks=checks, reason="all acceptance criteria met",
                validated_at=stamp,
            )
        return GoalValidationResult(
            status="evidence_missing", checks=checks, pending_approvals=pending,
            failed_steps=failed, budget_risk=budget_risk,
            reason="not all criteria satisfied", validated_at=stamp,
        )


# ═══════════════════════════════════════════════════════════════
# 循环检测
# ═══════════════════════════════════════════════════════════════


class LoopSignal(BaseModel):
    """循环信号：命中任意组合即终止。"""

    detected: bool = False
    kind: str = ""  # identical_plan / similar_recent / repeated_error / no_new_evidence
    detail: str = ""


def action_fingerprint(step_id: str, *, tool_name: str = "", args_hash: str = "") -> str:
    """动作指纹：步骤 + 工具 + 参数哈希（参数哈希已脱敏）。"""
    return f"{step_id}:{tool_name}:{args_hash}"


class LoopDetector:
    """组合循环检测：动作指纹、最近 N 步相似度、相同错误签名、无新增证据。"""

    def __init__(
        self,
        *,
        window: int = 5,
        similarity_threshold: float = 0.9,
        repeated_error_limit: int = 3,
        no_evidence_limit: int = 3,
    ):
        self.window = window
        self.similarity_threshold = similarity_threshold
        self.repeated_error_limit = repeated_error_limit
        self.no_evidence_limit = no_evidence_limit

    def _execution_summaries(self, state: RuntimeState) -> list[DecisionSummary]:
        """仅取实际执行阶段（phase=execute）的决策摘要，避免把单轮内部
        plan/policy/validate 摘要误判为重复执行。"""
        return [d for d in state.decision_summaries if d.phase == "execute"]

    def _recent_fingerprints(self, state: RuntimeState) -> list[str]:
        # 相似度只看 工具+参数 指纹（忽略 step_id，检测重复动作）
        return [
            d.tool_name + ":" + d.args_hash
            for d in self._execution_summaries(state)[-self.window:]
        ]

    def _recent_error_signatures(self, state: RuntimeState) -> list[str]:
        sigs = []
        for d in self._execution_summaries(state)[-self.window:]:
            if d.outcome in ("failed", "skipped"):
                sigs.append(f"{d.tool_name}:{d.result_hash}:{d.reason}")
        return sigs

    def detect_loop(self, state: RuntimeState) -> LoopSignal:
        exec_sums = self._execution_summaries(state)
        if len(exec_sums) < 2:
            return LoopSignal(detected=False)

        # 1. 相同计划重复（step_id 不前进）
        if len({s.step_id for s in exec_sums[-self.window:]}) <= 1:
            return LoopSignal(detected=True, kind="identical_plan",
                              detail="same step repeated without plan advance")

        # 2. 最近 N 步相似度（窗口内重复动作占多数即判定）
        #    相似度 = 与窗口内其它步共享指纹的步数占比（指纹=工具+参数哈希）
        fps = self._recent_fingerprints(state)
        counts: dict[str, int] = {}
        for f in fps:
            counts[f] = counts.get(f, 0) + 1
        similar = sum(1 for f in fps if counts[f] > 1)
        if len(fps) and similar / len(fps) >= self.similarity_threshold:
            return LoopSignal(detected=True, kind="similar_recent",
                              detail=f"recent {similar}/{len(fps)} actions share fingerprints")

        # 3. 无新增证据（连续无进展）
        last_n = exec_sums[-self.no_evidence_limit:]
        if len(last_n) >= self.no_evidence_limit and all(
            d.outcome != "success" for d in last_n
        ) and not state.evidence:
            return LoopSignal(detected=True, kind="no_new_evidence",
                              detail="no new evidence in recent steps")

        # 4. 相同错误签名
        errors = self._recent_error_signatures(state)
        if len(errors) >= self.repeated_error_limit and len(set(errors)) <= 1:
            return LoopSignal(detected=True, kind="repeated_error",
                              detail=f"same error signature repeated {len(errors)} times")
        return LoopSignal(detected=False)


# ═══════════════════════════════════════════════════════════════
# 终止判定
# ═══════════════════════════════════════════════════════════════


class TerminationDecision(BaseModel):
    stop: bool = False
    status: RuntimeStatus = RuntimeStatus.RUNNING
    reason: str = ""
    reason_code: str = ""


def decide_termination(
    state: RuntimeState,
    *,
    goal_result: GoalValidationResult | None = None,
    loop_signal: LoopSignal | None = None,
    user_canceled: bool = False,
    lease_lost: bool = False,
    policy_breaker: bool = False,
    no_executable_steps: bool = False,
    now: datetime | None = None,
) -> TerminationDecision:
    """强制停止条件判定；任一命中立即返回终态。"""
    stamp = now or datetime.now(UTC)

    if user_canceled:
        return TerminationDecision(stop=True, status=RuntimeStatus.CANCELED,
                                   reason="user canceled", reason_code="user_canceled")
    if lease_lost:
        return TerminationDecision(stop=True, status=RuntimeStatus.STOPPED,
                                   reason="run lease lost", reason_code="lease_lost")
    if policy_breaker:
        return TerminationDecision(stop=True, status=RuntimeStatus.STOPPED,
                                   reason="policy breaker tripped", reason_code="policy_breaker")

    if goal_result is not None and goal_result.status == "complete":
        return TerminationDecision(stop=True, status=RuntimeStatus.COMPLETED,
                                   reason="goal validated complete", reason_code="goal_complete")

    if state.usage.consecutive_failures >= state.budget.max_consecutive_failures:
        return TerminationDecision(stop=True, status=RuntimeStatus.FAILED,
                                   reason="consecutive failures threshold",
                                   reason_code="consecutive_failures")

    broken = state.usage.exceeded(state.budget, now=stamp)
    if broken:
        return TerminationDecision(stop=True, status=RuntimeStatus.BUDGET_EXCEEDED,
                                   reason=f"budget exceeded: {broken}", reason_code="budget_exceeded")

    if loop_signal is not None and loop_signal.detected:
        return TerminationDecision(stop=True, status=RuntimeStatus.STOPPED,
                                   reason=f"loop detected: {loop_signal.detail}", reason_code="loop_detected")

    if no_executable_steps:
        return TerminationDecision(stop=True, status=RuntimeStatus.STOPPED,
                                   reason="no executable steps", reason_code="no_executable_steps")

    return TerminationDecision(stop=False)
