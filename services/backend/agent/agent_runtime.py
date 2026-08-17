"""AgentRuntime 受控状态机 — 阶段四 4A Step 4A-6。

状态机：
    LOAD -> PLAN -> POLICY_CHECK -> EXECUTE -> OBSERVE
         -> VALIDATE -> CHECKPOINT
         -> CONTINUE | WAIT_APPROVAL | COMPLETE | STOP

每轮职责：
  1. 加载 RuntimeState、上下文（Skills/记忆由调用方 context_builder 注入）；
  2. 生成或修订结构化计划（planner 回调，返回 PlannedAction，服务端权威）；
  3. 校验计划 schema、依赖和 Worker 能力（planner 回调已约束，执行前再经 PolicyEngine）；
  4. 执行前经过 PolicyEngine（L0/L1 自动、L2 审批、L3 禁止）；
  5. 调用阶段三 Worker/Orchestrator（executor 回调包装 WorkerAdapter/Orchestrator）；
  6. 将结果规范化为 Observation 和证据；
  7. 更新预算与 decision_summary；
  8. 运行 GoalValidator 与 LoopDetector；
  9. 在有副作用的步骤前后写 Checkpoint 和 Step Ledger。

异常策略：
  - 可重试错误使用指数退避 + 抖动 + 最大次数（max_retries）；
  - 不可重试错误直接进入可解释终态（FAILED / STOPPED）；
  - 重启后从最后已提交检查点恢复（checkpointer 持久化 RuntimeState）；
  - 对不确定副作用先查询幂等结果（idempotency_key 预检查），不盲目重放；
  - 租约丢失后旧执行器立即停止写入（run(lease_lost=True)）。

安全约束：
  - 只保存可审计的 decision_summary，不记录模型私有思维链；
  - decision_summary / 事件中不含参数原文、密钥或提示词全文。
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from agent.goal_validator import (
    GoalValidator,
    LoopDetector,
    TerminationDecision,
    decide_termination,
)
from agent.policy_engine import (
    PolicyAction,
    PolicyDecision,
    PolicyEngine,
)
from agent.runtime_state import (
    DecisionSummary,
    EvidenceRecord,
    PendingApproval,
    RuntimeState,
    RuntimeStatus,
    ToolResultRecord,
)
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.agent_runtime")

# 状态机阶段（用于决策摘要与结果 trace）
PHASE_LOAD = "load"
PHASE_PLAN = "plan"
PHASE_POLICY = "policy"
PHASE_EXECUTE = "execute"
PHASE_OBSERVE = "observe"
PHASE_VALIDATE = "validate"
PHASE_CHECKPOINT = "checkpoint"

_MAX_DECISION_SUMMARIES = 200
_MAX_TOOL_RESULTS = 200


def _impact_scope_for(tool_name: str, risk_level: str) -> str:
    """审批影响范围（脱敏）：涉及的工具与风险等级，不含参数原文。"""
    return f"工具 {tool_name}（风险 {risk_level}）将执行外部副作用，需人工确认后放行"


class PlannedAction(BaseModel):
    """服务端可执行的结构化计划动作（模型只能影响字段值，不能绕过校验）。"""

    step_id: str = Field(..., min_length=1, max_length=64)
    tool_name: str = Field(..., min_length=1, max_length=100)
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=200)


class RuntimeResult(BaseModel):
    """自主运行最终结果（脱敏：不包含参数/推理/密钥）。"""

    run_id: str
    status: RuntimeStatus
    reason: str = ""
    reason_code: str = ""
    rounds: int = 0
    steps_run: int = 0
    completed_steps: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)
    evidence_count: int = 0
    decision_count: int = 0
    phases: list[str] = Field(default_factory=list)
    budget_usage: dict[str, Any] = Field(default_factory=dict)
    final_state: RuntimeState | None = None  # 供恢复/持久化使用


class AgentRuntimeError(Exception):
    """运行时装配错误（缺依赖等）。"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _hash_json(payload: dict[str, Any]) -> str:
    import hashlib
    import json

    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _backoff_delay(
    attempt: int, *, base: float = 1.0, multiplier: float = 2.0, jitter: float = 0.1
) -> float:
    """指数退避 + 抖动（attempt 从 1 开始：第 1 次重试 delay=base）。"""
    delay = base * (multiplier ** (attempt - 1))
    if jitter > 0:
        delay = delay * (1 + random.uniform(0, jitter))
    return round(delay, 3)


class AgentRuntime:
    """受约束自主 Agent 主循环。

    所有外部依赖通过构造参数注入（均可替换为测试桩）：
      - planner / executor / context_builder / checkpointer / ledger / event_emitter：async 回调
      - policy / goal_validator / loop_detector：策略对象（代码级不可变规则）
      - model_router：模型路由（advisory，只记录路由日志）
    """

    def __init__(
        self,
        *,
        planner: Callable[[RuntimeState], Awaitable[PlannedAction | None]] | None = None,
        executor: Callable[[RuntimeState, PlannedAction, dict[str, Any]], Awaitable[dict[str, Any]]]
        | None = None,
        policy: PolicyEngine | None = None,
        approval_service: Any = None,
        goal_validator: GoalValidator | None = None,
        loop_detector: LoopDetector | None = None,
        model_router: Any = None,
        context_builder: Callable[[RuntimeState], Awaitable[dict[str, Any]]] | None = None,
        checkpointer: Callable[[RuntimeState], Awaitable[Any]] | None = None,
        ledger: Callable[[RuntimeState, PlannedAction, dict[str, Any]], Awaitable[Any]]
        | None = None,
        event_emitter: Callable[[str, RuntimeState, dict[str, Any]], Awaitable[Any]] | None = None,
        max_retries: int = 2,
        backoff_base: float = 1.0,
        backoff_jitter: float = 0.1,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        if planner is None or executor is None:
            raise AgentRuntimeError("AgentRuntime requires planner and executor")
        self.planner = planner
        self.executor = executor
        self.policy = policy or PolicyEngine()
        self.approval_service = approval_service
        self.goal_validator = goal_validator or GoalValidator(
            required_artifact_keys=(), high_risk_requires_confirm=False
        )
        self.loop_detector = loop_detector or LoopDetector()
        self.model_router = model_router
        self.context_builder = context_builder
        self.checkpointer = checkpointer
        self.ledger = ledger
        self.event_emitter = event_emitter
        self.max_retries = max(0, max_retries)
        self.backoff_base = max(0.0, backoff_base)
        self.backoff_jitter = max(0.0, backoff_jitter)
        self._sleep = sleep or asyncio.sleep
        self._now_provider = now_provider or _utc_now

    # ═══════════════════════════════════════════════════════════
    # 主循环
    # ═══════════════════════════════════════════════════════════

    async def run(
        self,
        state: RuntimeState,
        *,
        cancel_event: asyncio.Event | None = None,
        lease_lost: bool = False,
        max_rounds: int = 100,
        now: datetime | None = None,
    ) -> RuntimeResult:
        """运行自主循环直到终态或暂停（WAIT_APPROVAL）。

        - cancel_event.set() → 安全点停止（CANCELED）；
        - lease_lost=True → 立即停止写入（STOPPED）；
        - 重启恢复：传入已持久化的 RuntimeState，从最后已提交检查点继续。
        """
        stamp = now or self._now_provider()
        if state.is_terminal:
            return self._result(
                state,
                rounds=0,
                phases=[],
                status=state.status,
                reason="already terminal",
                reason_code="terminal",
            )
        if state.status == RuntimeStatus.CANCEL_REQUESTED:
            # 重启前已请求取消：不再启动任何新调用，立即转为 CANCELED
            state, _ = state.transition_to(
                RuntimeStatus.CANCELED,
                reason="cancel requested before run",
                reason_code="user_canceled",
                now=stamp,
            )
            return self._result(
                state,
                rounds=0,
                phases=[],
                status=RuntimeStatus.CANCELED,
                reason="user canceled",
                reason_code="user_canceled",
            )
        state, _ = state.transition_to(RuntimeStatus.RUNNING, reason="runtime start", now=stamp)

        rounds = 0
        last_decision = TerminationDecision(stop=False)
        phases: list[str] = []
        while rounds < max_rounds:
            if cancel_event is not None and cancel_event.is_set():
                last_decision = decide_termination(state, user_canceled=True, now=stamp)
                break
            if lease_lost:
                last_decision = decide_termination(state, lease_lost=True, now=stamp)
                break
            if state.status == RuntimeStatus.CANCEL_REQUESTED:
                # 持久化的取消请求：API 已置 cancel_requested，安全点立即停止
                last_decision = decide_termination(state, user_canceled=True, now=stamp)
                break
            state, last_decision, round_phases = await self._one_round(
                state, cancel_event=cancel_event, lease_lost=lease_lost, now=now
            )
            rounds += 1
            phases.extend(round_phases)
            if last_decision.stop:
                break
            if state.is_terminal:
                break

        state = self._finalize(
            state, last_decision, rounds=rounds, max_rounds=max_rounds, now=stamp
        )
        await self._emit(
            "run_finished",
            state,
            {
                "status": state.status.value,
                "reason_code": last_decision.reason_code,
                "rounds": rounds,
            },
        )
        return self._result(
            state,
            rounds=rounds,
            phases=phases,
            status=state.status,
            reason=last_decision.reason,
            reason_code=last_decision.reason_code,
        )

    # ═══════════════════════════════════════════════════════════
    # 单轮
    # ═══════════════════════════════════════════════════════════

    async def _one_round(
        self,
        state: RuntimeState,
        *,
        cancel_event: asyncio.Event | None,
        lease_lost: bool,
        now: datetime,
    ) -> tuple[RuntimeState, TerminationDecision, list[str]]:
        stamp = now or self._now_provider()
        phases: list[str] = []

        # ── LOAD：上下文 / 记忆 / 模型路由 ─────────────────────
        context: dict[str, Any] = {}
        if self.context_builder is not None:
            try:
                context = await self.context_builder(state)
            except Exception as exc:
                logger.warning("[runtime] context builder failed: %s", exc)
        if self.model_router is not None:
            state = await self._route_model(state, context, stamp)
        phases.append(PHASE_LOAD)

        # ── 预算检查点 1：规划前 ──────────────────────────────
        if not state.usage.can_start_next_action(state.budget, now=stamp):
            decision = decide_termination(state, now=stamp)
            return state, decision, phases

        # ── PLAN：生成/修订结构化计划 ─────────────────────────
        action = await self.planner(state)
        if action is None:
            pause_status = getattr(self.planner, "pause_status", None)
            pause_reason = str(getattr(self.planner, "pause_reason", "") or "")
            pause_code = str(getattr(self.planner, "pause_reason_code", "") or "")
            if pause_status is not None:
                state = state.model_copy(
                    update={
                        "pending_questions": list(
                            getattr(self.planner, "pending_questions", ()) or ()
                        )[:20]
                    }
                )
                decision = TerminationDecision(
                    stop=True,
                    status=RuntimeStatus(pause_status),
                    reason=pause_reason or "runtime paused for user input",
                    reason_code=pause_code or "waiting_user",
                )
                return state, decision, phases
            state = self._record_decision(
                state,
                step_id=state.current_step,
                phase=PHASE_PLAN,
                action="end_plan",
                outcome="skipped",
                reason="no executable steps",
                now=stamp,
            )
            decision = TerminationDecision(
                stop=True,
                status=RuntimeStatus.STOPPED,
                reason="no executable steps",
                reason_code="no_executable_steps",
            )
            return state, decision, phases
        if not isinstance(action, PlannedAction):
            raise AgentRuntimeError(
                f"planner must return PlannedAction, got {type(action).__name__}"
            )
        state = state.model_copy(update={"current_step": action.step_id})
        state = self._record_decision(
            state,
            step_id=action.step_id,
            phase=PHASE_PLAN,
            action=f"plan:{action.tool_name}",
            outcome="planned",
            reason=action.note or "",
            now=stamp,
        )
        phases.append(PHASE_PLAN)
        await self._emit(
            "step_planned",
            state,
            {"step_id": action.step_id, "tool_name": action.tool_name, "note": action.note[:100]},
        )

        # ── POLICY_CHECK：执行前必经策略引擎 ───────────────────
        policy_decision = self.policy.evaluate(
            tool_name=action.tool_name,
            args=action.args,
            user_id=state.user_id,
            run_id=state.run_id,
            allowed_tool_names=state.budget.allowed_tool_names or None,
            usage=state.usage,
            budget=state.budget,
            now=stamp,
        )
        state = self._record_decision(
            state,
            step_id=action.step_id,
            phase=PHASE_POLICY,
            action=action.tool_name,
            tool_name=action.tool_name,
            args_hash=policy_decision.params_hash,
            result_hash=policy_decision.params_hash,
            outcome="approved" if policy_decision.allowed else "denied",
            reason=policy_decision.reason_code,
            now=stamp,
        )
        phases.append(PHASE_POLICY)
        await self._emit(
            "policy_checked",
            state,
            {
                "step_id": action.step_id,
                "tool_name": action.tool_name,
                "action": policy_decision.action.value,
                "reason_code": policy_decision.reason_code,
            },
        )

        approval_granted = False
        if not policy_decision.allowed:
            if policy_decision.action == PolicyAction.DENY:
                # PolicyEngine 熔断：立即进入可解释终态
                decision = TerminationDecision(
                    stop=True,
                    status=RuntimeStatus.STOPPED,
                    reason=f"policy breaker: {policy_decision.reason_code}",
                    reason_code="policy_breaker",
                )
                return state, decision, phases
            # REQUIRE_APPROVAL：恢复运行后若同一工具已有审批通过，消费一次性授权并放行
            if self.approval_service is not None:
                approved = next(
                    (
                        a
                        for a in state.approval_state.pending_approvals
                        if a.status == "approved"
                        and a.action == action.tool_name
                        and (not a.params_hash or a.params_hash == policy_decision.params_hash)
                    ),
                    None,
                )
                if approved is not None and self.approval_service.is_usable(approved, now=stamp):
                    tokens = list(state.approval_state.approved_tokens)
                    consumed = list(state.approval_state.consumed_tokens)
                    if self.approval_service.consume_token(
                        tokens, consumed, approved.one_time_token
                    ):
                        state = state.model_copy(
                            update={
                                "approval_state": state.approval_state.model_copy(
                                    update={
                                        "approved_tokens": tokens,
                                        "consumed_tokens": consumed,
                                        "pending_approvals": [
                                            a
                                            for a in state.approval_state.pending_approvals
                                            if a.approval_id != approved.approval_id
                                        ],
                                    }
                                )
                            }
                        )
                        state = self._record_decision(
                            state,
                            step_id=action.step_id,
                            phase=PHASE_POLICY,
                            action=action.tool_name,
                            tool_name=action.tool_name,
                            args_hash=policy_decision.params_hash,
                            result_hash=policy_decision.params_hash,
                            outcome="approved",
                            reason="approved_token_consumed",
                            now=stamp,
                        )
                        approval_granted = True
            if not approval_granted:
                # 登记待审批，暂停运行
                state = await self._register_approval(state, action, policy_decision, stamp)
                decision = TerminationDecision(
                    stop=True,
                    status=RuntimeStatus.WAITING_APPROVAL,
                    reason="waiting human approval",
                    reason_code="waiting_approval",
                )
                await self._emit(
                    "waiting_approval",
                    state,
                    {
                        "step_id": action.step_id,
                        "tool_name": action.tool_name,
                        "approval_id": state.approval_state.pending_approvals[-1].approval_id
                        if state.approval_state.pending_approvals
                        else "",
                    },
                )
                return state, decision, phases

        # ── EXECUTE：预算检查点 2（工具调用前）＋ 幂等预检查 ────
        if not state.usage.can_start_next_action(state.budget, now=stamp):
            decision = decide_termination(state, now=stamp)
            return state, decision, phases

        args = dict(action.args)
        idem_key = args.get("idempotency_key", "") or ""
        if idem_key:
            existing = next(
                (tr for tr in state.tool_results if tr.idempotency_key == idem_key), None
            )
            if existing is not None:
                # 对不确定副作用先查询幂等结果，不盲目重放
                state = self._record_decision(
                    state,
                    step_id=action.step_id,
                    phase=PHASE_EXECUTE,
                    action=action.tool_name,
                    tool_name=action.tool_name,
                    args_hash=policy_decision.params_hash,
                    result_hash=existing.result_hash,
                    outcome="success",
                    reason="idempotent replay skipped",
                    now=stamp,
                )
                return state, TerminationDecision(stop=False), phases

        state, result, tool_record = await self._execute_with_retry(
            state, action, context, policy_decision, stamp
        )
        phases.append(PHASE_EXECUTE)

        # ── OBSERVE：结果规范化 → 证据 / 决策摘要 ─────────────
        state = self._observe(state, action, result, tool_record, stamp)
        phases.append(PHASE_OBSERVE)
        success = self._is_success(result)
        await self._emit(
            "tool_executed",
            state,
            {
                "step_id": action.step_id,
                "tool_name": action.tool_name,
                "ok": success,
                "duration_ms": tool_record.duration_ms,
                "result_hash": tool_record.result_hash,
                "error_code": tool_record.error_code,
            },
        )

        # 不可重试错误：直接进入可解释终态（FAILED），不再继续下一轮
        if not success and not bool(result.get("retryable", True)):
            reason = str(
                result.get("error", "") or result.get("error_code", "") or "non-retryable error"
            )[:200]
            decision = TerminationDecision(
                stop=True,
                status=RuntimeStatus.FAILED,
                reason=f"non-retryable error: {reason}",
                reason_code="non_retryable_error",
            )
            await self._emit(
                "step_failed",
                state,
                {
                    "step_id": action.step_id,
                    "tool_name": action.tool_name,
                    "error_code": tool_record.error_code,
                    "retryable": False,
                },
            )
            return state, decision, phases

        # ── VALIDATE：GoalValidator + LoopDetector ─────────────
        goal_result = self.goal_validator.validate(
            state, artifacts=self._collect_artifacts(state), now=stamp
        )
        loop_signal = self.loop_detector.detect_loop(state)
        state = self._record_decision(
            state,
            step_id=action.step_id,
            phase=PHASE_VALIDATE,
            action="validate",
            outcome=goal_result.status,
            reason=goal_result.reason[:200],
            now=stamp,
        )
        phases.append(PHASE_VALIDATE)

        # ── CHECKPOINT：副作用前后写检查点 / 账本 ──────────────
        await self._checkpoint(state, action, tool_record)
        phases.append(PHASE_CHECKPOINT)

        # ── 终止判定 ──────────────────────────────────────────
        decision = decide_termination(
            state,
            goal_result=goal_result,
            loop_signal=loop_signal,
            user_canceled=bool(cancel_event is not None and cancel_event.is_set()),
            lease_lost=lease_lost,
            now=stamp,
        )
        return state, decision, phases

    # ═══════════════════════════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════════════════════════

    async def _route_model(
        self, state: RuntimeState, context: dict[str, Any], stamp: datetime
    ) -> RuntimeState:
        """模型路由（advisory）：只记录模型标识 + 原因码，不记录敏感正文。"""
        try:
            from agent.model_router import RouteRequest, SensitivityLevel

            request = RouteRequest(
                task_type="decide",
                sensitivity=SensitivityLevel.L0,
                context_chars=int(context.get("context_chars", 0) or 0),
                remaining_input_tokens=state.budget.max_input_tokens - state.usage.input_tokens,
                remaining_output_tokens=state.budget.max_output_tokens - state.usage.output_tokens,
                remaining_cost_usd=state.budget.max_cost_usd - state.usage.cost_usd,
                allow_downgrade=True,
            )
            decision = self.model_router.route(request)
            return self._record_decision(
                state,
                step_id=state.current_step,
                phase=PHASE_LOAD,
                action="model_route",
                outcome="ok",
                reason=f"model={decision.model}:{decision.reason_code}",
                now=stamp,
            )
        except Exception as exc:
            logger.warning("[runtime] model route failed: %s", exc)
            return self._record_decision(
                state,
                step_id=state.current_step,
                phase=PHASE_LOAD,
                action="model_route",
                outcome="failed",
                reason=f"model_route_error:{type(exc).__name__}",
                now=stamp,
            )

    async def _register_approval(
        self,
        state: RuntimeState,
        action: PlannedAction,
        policy_decision: PolicyDecision,
        stamp: datetime,
    ) -> RuntimeState:
        """登记人工审批项（无 approval_service 时仍记录待审批占位）。"""
        approval = None
        if self.approval_service is not None:
            approval = await self.approval_service.request(
                run_id=state.run_id,
                action=action.tool_name,
                params_hash=policy_decision.params_hash,
                params_summary=policy_decision.params_summary,
                risk_level=policy_decision.risk_level,
                trigger_rule=policy_decision.reason_code,
                decision_summary_id="",
                impact_scope=_impact_scope_for(
                    action.tool_name, policy_decision.risk_level.value
                ),
                now=stamp,
            )
        else:
            approval = PendingApproval(
                approval_id="ap-" + uuid.uuid4().hex[:12],
                action=action.tool_name,
                risk_level=policy_decision.risk_level.value,
                impact_scope=_impact_scope_for(
                    action.tool_name, policy_decision.risk_level.value
                ),
                params_hash=policy_decision.params_hash,
                params_summary=policy_decision.params_summary,
                trigger_rule=policy_decision.reason_code,
                status="pending",
                expires_at=None,
            )
        new_state = state.model_copy(
            update={
                "approval_state": state.approval_state.model_copy(
                    update={
                        "pending_approvals": [*state.approval_state.pending_approvals, approval]
                    }
                )
            }
        )
        return self._record_decision(
            new_state,
            step_id=action.step_id,
            phase=PHASE_POLICY,
            action=action.tool_name,
            tool_name=action.tool_name,
            args_hash=approval.params_hash,
            result_hash="",
            outcome="blocked",
            reason="requires_approval",
            now=stamp,
        )

    async def _execute_with_retry(
        self,
        state: RuntimeState,
        action: PlannedAction,
        context: dict[str, Any],
        policy_decision: PolicyDecision,
        stamp: datetime,
    ) -> tuple[RuntimeState, dict[str, Any], ToolResultRecord]:
        """执行（指数退避 + 抖动 + 最大次数）。可重试错误重试；不可重试直接失败。"""
        usage = state.usage
        attempt = 1
        result: dict[str, Any] = {"ok": False, "error": "not executed"}
        while attempt <= self.max_retries + 1:
            if attempt > 1:
                usage.record_retry(now=stamp)
                delay = _backoff_delay(attempt, base=self.backoff_base, jitter=self.backoff_jitter)
                if delay > 0:
                    await self._sleep(delay)
            meta = {"context": context, "attempt": attempt, "policy": policy_decision}
            result = await self.executor(state, action, meta)
            if self._is_success(result) or not bool(result.get("retryable", True)):
                # 成功或不可重试错误：直接退出重试循环，进入可解释终态判定
                break
            attempt += 1

        success = self._is_success(result)
        usage.record_tool_call(now=stamp)
        usage.record_step(
            tokens_in=int(result.get("tokens_in", 0) or 0),
            tokens_out=int(result.get("tokens_out", 0) or 0),
            cost=float(result.get("cost_usd", 0.0) or 0.0),
            now=stamp,
        )
        if success:
            usage.record_success(now=stamp)
        else:
            usage.record_failure(now=stamp)
        state = state.model_copy(update={"usage": usage})

        tool_record = ToolResultRecord(
            tool_id="tool-" + uuid.uuid4().hex[:12],
            display_name=action.tool_name,
            ok=success,
            error_code=str(result.get("error_code", "") or "") if not success else "",
            args_hash=policy_decision.params_hash,
            result_hash=str(result.get("result_hash", "") or _hash_json({"ok": success})),
            duration_ms=int(result.get("duration_ms", 0) or 0),
            idempotency_key=str(action.args.get("idempotency_key", "") or ""),
            source_ids=list(result.get("source_ids", []) or []),
            created_at=stamp,
        )
        return state, result, tool_record

    def _observe(
        self,
        state: RuntimeState,
        action: PlannedAction,
        result: dict[str, Any],
        tool_record: ToolResultRecord,
        stamp: datetime,
    ) -> RuntimeState:
        """把执行结果规范化为 Observation 与证据。"""
        success = self._is_success(result)
        tool_results = [*state.tool_results, tool_record]
        if len(tool_results) > _MAX_TOOL_RESULTS:
            tool_results = tool_results[-_MAX_TOOL_RESULTS:]

        evidence = list(state.evidence)
        for item in result.get("evidence", []) or []:
            if not isinstance(item, dict):
                continue
            evidence.append(
                EvidenceRecord(
                    evidence_id="ev-" + uuid.uuid4().hex[:12],
                    step_id=action.step_id,
                    acceptance_index=item.get("acceptance_index"),
                    kind=str(item.get("kind", "tool_result")),
                    ref=tool_record.tool_id,
                    hash=tool_record.result_hash,
                    note=str(item.get("note", "") or "")[:200],
                    created_at=stamp,
                )
            )

        updated = state.model_copy(
            update={
                "tool_results": tool_results,
                "normalized_observations": {
                    **state.normalized_observations,
                    **(
                        {action.step_id: result["normalized_observation"]}
                        if isinstance(result.get("normalized_observation"), dict)
                        else {}
                    ),
                },
                "evidence": evidence,
                "completed_steps": [*state.completed_steps, action.step_id]
                if success
                else state.completed_steps,
                "failed_steps": [*state.failed_steps, action.step_id]
                if not success
                else state.failed_steps,
            }
        )
        return self._record_decision(
            updated,
            step_id=action.step_id,
            phase=PHASE_EXECUTE,
            action=action.tool_name,
            tool_name=action.tool_name,
            args_hash=tool_record.args_hash,
            result_hash=tool_record.result_hash,
            outcome="success" if success else "failed",
            reason=str(result.get("error", "") or result.get("error_code", "") or "")[:200]
            if not success
            else "",
            now=stamp,
        )

    def _collect_artifacts(self, state: RuntimeState) -> dict[str, Any]:
        """从已完成步骤中收集产物（当前为最近一次成功步骤输出占位，供 GoalValidator 使用）。"""
        artifacts: dict[str, Any] = {}
        if state.tool_results:
            latest = state.tool_results[-1]
            artifacts["last_tool"] = latest.display_name
            artifacts["last_tool_ok"] = latest.ok
        return artifacts

    async def _checkpoint(
        self,
        state: RuntimeState,
        action: PlannedAction,
        tool_record: ToolResultRecord,
    ) -> None:
        """副作用步骤前后写检查点与账本（失败仅记日志，不影响终止判定）。"""
        if self.checkpointer is not None:
            try:
                await self.checkpointer(state)
            except Exception as exc:
                logger.warning("[runtime] checkpoint failed: %s", exc)
        if self.ledger is not None and (tool_record.idempotency_key or not tool_record.ok):
            try:
                await self.ledger(state, action, {"tool_record": tool_record})
            except Exception as exc:
                logger.warning("[runtime] ledger write failed: %s", exc)

    async def _emit(self, event_type: str, state: RuntimeState, payload: dict[str, Any]) -> None:
        if self.event_emitter is None:
            return
        try:
            await self.event_emitter(event_type, state, payload)
        except Exception as exc:
            logger.warning("[runtime] emit %s failed: %s", event_type, exc)

    def _record_decision(
        self,
        state: RuntimeState,
        *,
        step_id: str,
        phase: str,
        action: str,
        outcome: str,
        reason: str = "",
        tool_name: str = "",
        args_hash: str = "",
        result_hash: str = "",
        now: datetime | None = None,
    ) -> RuntimeState:
        stamp = now or self._now_provider()
        summaries = [
            *state.decision_summaries,
            DecisionSummary(
                step_id=step_id,
                phase=phase,
                action=action[:100],
                tool_name=tool_name[:100],
                args_hash=args_hash,
                result_hash=result_hash,
                outcome=outcome[:32],
                reason=reason[:200],
                created_at=stamp,
            ),
        ]
        if len(summaries) > _MAX_DECISION_SUMMARIES:
            summaries = summaries[-_MAX_DECISION_SUMMARIES:]
        return state.model_copy(update={"decision_summaries": summaries})

    @staticmethod
    def _is_success(result: dict[str, Any]) -> bool:
        return bool(result and result.get("ok", False))

    def _finalize(
        self,
        state: RuntimeState,
        decision: TerminationDecision,
        *,
        rounds: int,
        max_rounds: int,
        now: datetime,
    ) -> RuntimeState:
        """写入终态（或 WAIT_APPROVAL 暂停态）。"""
        if state.is_terminal:
            return state
        if decision.stop:
            target = decision.status
            if target == RuntimeStatus.RUNNING or target is None:
                target = RuntimeStatus.STOPPED
            state, _ = state.transition_to(
                target, reason=decision.reason, reason_code=decision.reason_code, now=now
            )
        elif rounds >= max_rounds:
            state, _ = state.transition_to(
                RuntimeStatus.STOPPED,
                reason="max rounds reached",
                reason_code="max_rounds",
                now=now,
            )
        return state

    def _result(
        self,
        state: RuntimeState,
        *,
        rounds: int,
        phases: list[str],
        status: RuntimeStatus,
        reason: str = "",
        reason_code: str = "",
    ) -> RuntimeResult:
        return RuntimeResult(
            run_id=state.run_id,
            status=status,
            reason=reason,
            reason_code=reason_code,
            rounds=rounds,
            steps_run=len(state.completed_steps) + len(state.failed_steps),
            completed_steps=list(state.completed_steps),
            failed_steps=list(state.failed_steps),
            evidence_count=len(state.evidence),
            decision_count=len(state.decision_summaries),
            phases=phases,
            budget_usage={
                "steps": state.usage.steps,
                "input_tokens": state.usage.input_tokens,
                "output_tokens": state.usage.output_tokens,
                "tool_calls": state.usage.tool_calls,
                "retries": state.usage.retries,
                "cost_usd": state.usage.cost_usd,
            },
            final_state=state,
        )
