"""Agent Loop 状态机运行时 -- 阶段1 1.1 节（WBS 1.1）。

统一状态机：
    ADMIT -> LOAD_CONTEXT -> THINK/PLAN -> RESERVE_BUDGET -> POLICY_CHECK
    -> EXECUTE -> OBSERVE -> VALIDATE -> CHECKPOINT
    -> REPLAN | FINALIZE | WAIT_APPROVAL | STOP

设计要点（对齐 00-统一架构 与 02-阶段1）：
  - "模型未返回工具调用"不再自动视为成功：必须通过输出非空验证，
    并提供可选 output_schema / acceptance_criteria / validate_answer；
  - 每次 LLM 调用前经 BudgetManager 预留预算，调用后按 provider usage 结算，
    未用部分释放；finalization 必须有预留额度，不允许预算旁路；
  - 同轮多工具经 ToolExecutor 真实并发执行（依赖分组 + 四层 semaphore），
    按原 tool call 顺序回填；一个工具失败不取消兄弟工具；
  - LoopDetector 六类无进展检测：首次命中受控 replan，再次命中停止；
  - 工具结果经 ContextCompressor 去重/截断，ToolResultCache 按权限边界缓存；
  - 所有事件经 AgentEventStore 落库（可选），写入失败不影响 Loop 执行。

向后兼容：构造参数与 run() 签名保持阶段一旧实现语义，保证既有
tests/unit/test_agent_loop.py 与 draft_chat._answer_agent 零改动。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from agent.agent_contracts import (
    AgentEvent,
    BudgetUsage,
    EventType,
    LoopBudget,
    LoopResult,
    LoopStatus,
    RunContext,
    ToolPolicy,
)
from agent.agent_event_store import AgentEventStore
from agent.budget_manager import (
    BudgetManager,
    BudgetPlan,
    BudgetStatus,
    ConcurrencyLimiter,
    ReservationKind,
)
from agent.context_optimizer import ContextCompressor, ToolResultCache
from agent.llm_wrapper import LLMWrapper, UnsupportedToolCallError
from agent.loop_detector import LoopDetector
from agent.retry import RetryState
from agent.tool_executor import ToolExecutionResult, ToolExecutor
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger("backend.agent.agent_loop")


class LoopPhase(StrEnum):
    """Agent Loop 状态机阶段。"""

    ADMIT = "admit"
    LOAD_CONTEXT = "load_context"
    THINK_PLAN = "think_plan"
    RESERVE_BUDGET = "reserve_budget"
    POLICY_CHECK = "policy_check"
    EXECUTE = "execute"
    OBSERVE = "observe"
    VALIDATE = "validate"
    CHECKPOINT = "checkpoint"
    REPLAN = "replan"
    FINALIZE = "finalize"
    WAIT_APPROVAL = "wait_approval"
    STOP = "stop"


# AgentEvent -> EventEnvelope 事件类型映射（阶段1 事件契约）
_EVENT_TYPE_MAP = {
    EventType.LOOP_START: "loop_started",
    EventType.LOOP_END: "loop_ended",
    EventType.TOOL_STARTED: "tool_started",
    EventType.TOOL_FINISHED: "tool_finished",
    EventType.TOOL_FAILED: "tool_failed",
    EventType.TOOL_BLOCKED: "tool_blocked",
    EventType.FINALIZATION: "finalization_started",
}

# LoopPhase -> EventEnvelope.phase（AgentEventStore.Phase Literal）
_PHASE_MAP = {
    LoopPhase.ADMIT: "admit",
    LoopPhase.LOAD_CONTEXT: "load_context",
    LoopPhase.THINK_PLAN: "think",
    LoopPhase.RESERVE_BUDGET: "budget",
    LoopPhase.POLICY_CHECK: "policy",
    LoopPhase.EXECUTE: "execute",
    LoopPhase.OBSERVE: "observe",
    LoopPhase.VALIDATE: "validate",
    LoopPhase.CHECKPOINT: "checkpoint",
    LoopPhase.REPLAN: "replan",
    LoopPhase.FINALIZE: "finalize",
    LoopPhase.WAIT_APPROVAL: "wait_approval",
    LoopPhase.STOP: "stop",
}


def _loop_budget_to_plan(budget: LoopBudget, run_context: RunContext) -> BudgetPlan:
    """将旧 LoopBudget 映射为统一 BudgetPlan（对齐统一预算模型）。"""
    return BudgetPlan(
        max_input_tokens=budget.max_input_tokens,
        max_output_tokens=budget.max_output_tokens,
        max_cost_usd=budget.max_cost_usd,
        max_steps=budget.max_rounds,
        max_tool_calls=budget.max_tool_calls,
        max_runtime_seconds=budget.deadline_seconds,
        max_parallel_tools=budget.max_parallel_tools,
        deadline_at=run_context.deadline_at,
    )


class AgentLoop:
    """有界 Agent Loop 状态机运行时（阶段1 1.1）。

    Args:
        llm_wrapper: LLMWrapper 实例（决策轮非流式调用 + usage 解析）
        tools: 工具列表（langchain @tool 或 mock，需有 .name/.ainvoke）
        budget: LoopBudget（映射为 BudgetPlan）
        run_context: 单次运行上下文（身份/权限/deadline）
        tool_policies: {工具名: ToolPolicy}；缺省按 budget 超时生成
        max_repeated_actions: 相同 (工具, 参数) 出现超过该次数则拦截（兼容旧行为）
        event_store: 可选 AgentEventStore（统一事件落库）
        tool_result_cache: 可选 ToolResultCache（按权限边界缓存工具结果）
        model_router: 可选 ModelRouter（记录 route decision，不切换实例）
        budget_plan: 可选 BudgetPlan（覆盖默认映射）
    """

    def __init__(
        self,
        *,
        llm_wrapper: LLMWrapper,
        tools: list,
        budget: LoopBudget,
        run_context: RunContext,
        tool_policies: dict[str, ToolPolicy] | None = None,
        max_repeated_actions: int = 2,
        event_store: AgentEventStore | None = None,
        tool_result_cache: ToolResultCache | None = None,
        model_router: Any | None = None,
        budget_plan: BudgetPlan | None = None,
        metrics: Any | None = None,
    ):
        self.llm_wrapper = llm_wrapper
        self.tools_by_name: dict[str, Any] = {t.name: t for t in tools}
        self.budget = budget
        self.run_context = run_context
        self.max_repeated_actions = max_repeated_actions
        self.metrics = metrics  # MetricCollector（可选）：run 结束喂生产指标

        # 缺省策略：按 budget 的工具超时生成，保持旧行为
        self.tool_policies = tool_policies or {
            name: ToolPolicy(name=name, timeout_seconds=budget.tool_timeout_seconds)
            for name in self.tools_by_name
        }

        # 阶段1 组件装配
        self.budget_plan = budget_plan or _loop_budget_to_plan(budget, run_context)
        self.limiter = ConcurrencyLimiter()
        self.budget_manager = BudgetManager(
            plan=self.budget_plan,
            user_id=run_context.user_id,
            tenant_id=run_context.tenant_id or "",
            limiter=self.limiter,
            on_event=self._on_budget_event,
        )
        self.detector = LoopDetector()
        self.compressor = ContextCompressor()
        self.tool_result_cache = tool_result_cache
        self.event_store = event_store
        self.model_router = model_router

        self.executor = ToolExecutor(
            tools_by_name=self.tools_by_name,
            tool_policies=self.tool_policies,
            max_parallel_tools=budget.max_parallel_tools,
            limiter=self.limiter,
            run_context=run_context,
            tenant_id=run_context.tenant_id or "",
            detector=None,  # LoopDetector 由 Loop 统一观察，避免重复计数
            on_event=self._on_tool_event,
        )

        # run 期状态（每次 run() 重置）
        self.phase = LoopPhase.ADMIT
        self._events: list[AgentEvent] = []
        self._seq = 0
        self._pending_store: list[dict[str, Any]] = []
        self.contract_usage = BudgetUsage()
        self.action_history: list[tuple[str, str]] = []
        self.tool_names_used: list[str] = []
        self.references: list[str] = []
        self._route_decision: str = ""

    # ── 状态机入口 ──────────────────────────────────────────

    async def run(
        self,
        *,
        system_prompt: str,
        user_message: str,
        initial_context: str = "",
        acceptance_criteria: list[str] | None = None,
        output_schema: type[Any] | None = None,
        validate_answer: Callable[[str], tuple[bool, str]] | None = None,
    ) -> LoopResult:
        """执行 Agent Loop 状态机。

        Args:
            system_prompt: 系统提示词
            user_message: 用户消息
            initial_context: 初始上下文（文章/草稿摘要，可选）
            acceptance_criteria: 验收条件（VALIDATE 阶段检查，可选）
            output_schema: 输出 Pydantic Schema（VALIDATE 阶段检查，可选）
            validate_answer: 自定义答案校验器 (answer) -> (ok, reason)，可选

        Returns:
            LoopResult
        """
        try:
            result = await self._run_inner(
                system_prompt=system_prompt,
                user_message=user_message,
                initial_context=initial_context,
                acceptance_criteria=acceptance_criteria,
                output_schema=output_schema,
                validate_answer=validate_answer,
            )
        finally:
            await self._flush_events()
        self._record_run_metrics(result)
        return result

    def _record_run_metrics(self, result: LoopResult) -> None:
        """run 结束喂生产指标（脱敏数值，供告警规则消费）。

        原语计数 → production_alert_metrics 聚合为告警规则输入键。
        """
        if self.metrics is None:
            return
        usage = self.budget_manager.usage
        self.metrics.inc("run_finished_total")
        if result.status == LoopStatus.COMPLETED:
            self.metrics.inc("run_completed_total")
        elif result.status == LoopStatus.BUDGET_EXCEEDED:
            self.metrics.inc("run_budget_exhausted_total")
            self.metrics.inc("budget_exhausted")
        elif result.status == LoopStatus.CANCELLED:
            self.metrics.inc("run_cancelled_total")
        else:
            self.metrics.inc("run_degraded_total")
        if usage.total_tokens:
            self.metrics.inc("llm_tokens_total", amount=usage.total_tokens)
        if usage.cost_usd > 0:
            self.metrics.inc("cost_usd_micro_total", amount=int(usage.cost_usd * 1_000_000))
        if usage.tool_calls:
            self.metrics.inc("tool_calls_total", amount=usage.tool_calls)
        if usage.retries:
            self.metrics.inc("retries_total", amount=usage.retries)
        error_events = sum(1 for e in self._events if e.type == EventType.DEGRADE)
        if error_events:
            self.metrics.inc("error_events_total", amount=error_events)
        self.metrics.observe("run_duration_seconds", value=usage.elapsed_seconds())

    async def _run_inner(
        self,
        *,
        system_prompt: str,
        user_message: str,
        initial_context: str,
        acceptance_criteria: list[str] | None,
        output_schema: type[Any] | None,
        validate_answer: Callable[[str], tuple[bool, str]] | None,
    ) -> LoopResult:
        # ADMIT：构建消息与绑定工具
        self._enter_phase(LoopPhase.ADMIT)
        messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
        if initial_context:
            messages.append(
                HumanMessage(content=f"上下文:\n{initial_context}\n\n用户问题: {user_message}")
            )
        else:
            messages.append(HumanMessage(content=user_message))
        self._emit(EventType.LOOP_START)

        bound_llm = self.llm_wrapper.llm.bind_tools(list(self.tools_by_name.values()))

        # LOAD_CONTEXT：初始上下文已注入（历史摘要压缩在上下文侧完成）
        self._enter_phase(LoopPhase.LOAD_CONTEXT)

        cancelled = False
        finalize_needed = False
        finalize_reason = ""
        validate_fail_count = 0
        unsupported_tool_replans = 0
        loop_stopped = False

        while True:
            # CHECKPOINT：deadline / 预算水位 / 取消
            self._enter_phase(LoopPhase.CHECKPOINT)
            if self.run_context.is_expired():
                cancelled = True
                break
            if not self.budget_manager.can_continue():
                finalize_needed = True
                finalize_reason = self.budget_manager.exhausted_reason() or "budget_exhausted"
                break
            budget_status = self.budget_manager.status()
            if budget_status in (BudgetStatus.WARNING, BudgetStatus.COMPRESS):
                self._emit(
                    EventType.BUDGET_WARNING,
                    phase="checkpoint",
                    error_code=f"water_level:{budget_status.value}",
                )

            round_no = self.contract_usage.rounds
            self.contract_usage.rounds += 1
            self.budget_manager.record_step()
            self._emit(EventType.ROUND_START, phase="execute", round_no=round_no)

            # THINK/PLAN + 模型路由记录（不切换实例）
            self._enter_phase(LoopPhase.THINK_PLAN)
            self._record_route_decision(messages)
            model_id = self._route_decision or self._model_name()

            # RESERVE_BUDGET：LLM 调用前预留
            self._enter_phase(LoopPhase.RESERVE_BUDGET)
            reservation = await self.budget_manager.reserve(
                kind=ReservationKind.LLM,
                model_id=model_id,
            )
            if reservation is None:
                finalize_needed = True
                finalize_reason = "no_budget_for_round"
                break

            # LLM 决策轮调用（重试次数受 max_retries 预算约束）
            retry_state = RetryState()
            try:
                response = await self.llm_wrapper.invoke_agent_step(
                    bound_llm=bound_llm,
                    messages=messages,
                    run_context=self.run_context,
                    loop_round=round_no,
                    max_attempts=max(1, self.budget_plan.max_retries + 1),
                    retry_state_out=retry_state,
                )
                # RetryState.attempts 只记录失败尝试（成功路径立即返回不记录），
                # 因此 total_attempts 即实际重试次数
                retries_used = retry_state.total_attempts
                if retries_used:
                    self.budget_manager.record_retry(tokens_used=0)
            except asyncio.CancelledError:
                self.budget_manager.release(reservation)
                cancelled = True
                break
            except UnsupportedToolCallError as exc:
                # 模型返回绑定工具列表之外的函数名（幻觉/近似名）：
                # 提示重规划（最多 2 次），避免整个运行崩溃
                self._enter_phase(LoopPhase.OBSERVE)
                logger.warning(
                    "[%s] unsupported tool call on round %d: %s",
                    self.run_context.trace_id,
                    round_no,
                    exc,
                )
                self.budget_manager.settle_llm(
                    reservation,
                    input_tokens=0,
                    output_tokens=0,
                    usage_estimated=True,
                    reason_code="unsupported_tool",
                    attribution="primary",
                )
                self._emit(
                    EventType.DEGRADE,
                    phase="observe",
                    round_no=round_no,
                    error_code="unsupported_tool_call",
                )
                unsupported_tool_replans += 1
                if unsupported_tool_replans >= 2:
                    finalize_needed = True
                    finalize_reason = "unsupported_tool_call"
                    break
                messages.append(
                    HumanMessage(
                        content=(
                            "[工具提示] 你尝试调用了不可用的工具。"
                            "请仅使用系统提供的工具名重新规划本次回答。"
                        )
                    )
                )
                self.detector.reset()
                continue
            except Exception as exc:
                # OBSERVE：失败调用也要结算（记录浪费 token），随后降级 finalize
                self._enter_phase(LoopPhase.OBSERVE)
                logger.exception(
                    "[%s] LLM step failed on round %d", self.run_context.trace_id, round_no
                )
                self.budget_manager.settle_llm(
                    reservation,
                    input_tokens=0,
                    output_tokens=0,
                    usage_estimated=True,
                    reason_code="failed",
                    attribution="primary",
                )
                self._emit(
                    EventType.DEGRADE,
                    phase="observe",
                    round_no=round_no,
                    error_code=type(exc).__name__,
                )
                finalize_needed = True
                finalize_reason = f"LLM failed on round {round_no}: {type(exc).__name__}"
                break

            # OBSERVE：按 provider usage 结算
            self._enter_phase(LoopPhase.OBSERVE)
            in_t, out_t, cached_t, estimated = self.llm_wrapper._resolve_usage(messages, response)
            self.budget_manager.settle_llm(
                reservation,
                input_tokens=in_t,
                output_tokens=out_t,
                cached_input_tokens=cached_t,
                usage_estimated=estimated,
                reason_code="ok",
                attribution="primary",
            )
            self.contract_usage.input_tokens += in_t
            self.contract_usage.output_tokens += out_t
            self.contract_usage.cost_usd = self.budget_manager.usage.cost_usd

            tool_calls = getattr(response, "tool_calls", []) or []
            if not tool_calls:
                # VALIDATE：输出非空 + schema/criteria/validator
                self._enter_phase(LoopPhase.VALIDATE)
                answer = (
                    response.content if isinstance(response.content, str) else str(response.content)
                )
                answer = answer.strip()
                ok, reason = self._validate_output(
                    answer,
                    acceptance_criteria=acceptance_criteria,
                    output_schema=output_schema,
                    validate_answer=validate_answer,
                )
                if ok:
                    self._emit(EventType.ROUND_END, phase="validate", round_no=round_no)
                    self._enter_phase(LoopPhase.STOP)
                    self._emit(EventType.LOOP_END, phase="stop", round_no=round_no)
                    return self._result(
                        status=LoopStatus.COMPLETED,
                        answer=answer,
                    )
                # 验证失败：首次 replan，再次 stop 并返回降级结果
                validate_fail_count += 1
                self._emit(
                    EventType.DEGRADE,
                    phase="validate",
                    round_no=round_no,
                    error_code=f"validate_failed:{reason}",
                )
                if validate_fail_count >= 2:
                    loop_stopped = True
                    finalize_needed = True
                    finalize_reason = f"validate_failed:{reason}"
                    break
                self._enter_phase(LoopPhase.REPLAN)
                messages.append(response)
                messages.append(
                    HumanMessage(
                        content=(
                            f"[验证提示] 你的回答未通过验证（{reason}）。"
                            "请重新回答，确保输出非空且满足要求，或继续使用工具。"
                        )
                    )
                )
                self.detector.reset()
                continue

            # EXECUTE：同轮多工具（校验 -> 并发执行 -> 按原顺序回填）
            self._enter_phase(LoopPhase.POLICY_CHECK)
            messages.append(response)  # AIMessage（含 tool_calls）

            tool_calls_filtered: list[dict[str, Any]] = []
            repeated_results: dict[str, ToolExecutionResult] = {}
            pending: dict[str, Any] = {}
            for tc in tool_calls:
                name = str(tc.get("name", ""))
                call_id = str(tc.get("id", ""))
                args = tc.get("args", {}) or {}
                args_hash = self._args_hash(args)
                self.action_history.append((name, args_hash))
                if self.action_history.count((name, args_hash)) > self.max_repeated_actions:
                    repeated_results[call_id] = ToolExecutionResult.blocked(
                        tool_call_id=call_id,
                        tool_name=name,
                        error_code="repeated_action",
                        message=(
                            f"[重复操作] {name} 已执行相同操作 {self.max_repeated_actions + 1} 次，"
                            "请更换策略或直接回答"
                        ),
                    )
                    if self.metrics is not None:
                        self.metrics.inc("duplicate_side_effect_count")
                    continue
                tool_calls_filtered.append(tc)
                rsv = await self.budget_manager.reserve(
                    kind=ReservationKind.TOOL,
                    tool_name=name,
                    model_id=model_id,
                )
                if rsv is not None:
                    pending[call_id] = rsv

            # 工具结果缓存命中替换（执行前，按权限边界）
            cache_hits: dict[str, ToolExecutionResult] = {}
            if self.tool_result_cache is not None:
                for tc in tool_calls_filtered:
                    name = str(tc.get("name", ""))
                    call_id = str(tc.get("id", ""))
                    cached = self.tool_result_cache.get(
                        tenant_id=self.run_context.tenant_id or "",
                        user_id=self.run_context.user_id,
                        tool_name=name,
                        args=tc.get("args", {}) or {},
                    )
                    if cached is not None:
                        cache_hits[call_id] = ToolExecutionResult(
                            tool_call_id=call_id,
                            tool_name=name,
                            ok=True,
                            message=cached.content,
                            result_hash=cached.result_hash,
                            source_ids=list(cached.source_ids),
                            truncated=True,
                        )
                        rsv = pending.pop(call_id, None)
                        if rsv is not None:
                            self.budget_manager.release(rsv)
            to_execute = [tc for tc in tool_calls_filtered if tc.get("id") not in cache_hits]

            def reserve_cb(spec: Any, _pending: dict[str, Any] = pending) -> Any | None:
                return _pending.pop(spec.tool_call_id, None)

            self.executor.budget_reserve = reserve_cb
            remaining = max(
                0, self.budget_plan.max_tool_calls - self.budget_manager.usage.tool_calls
            )
            executed = await self.executor.execute_many(to_execute, remaining_tool_budget=remaining)

            # 合并缓存命中与执行结果（按原 tool call 顺序回填）
            by_id: dict[str, ToolExecutionResult] = {r.tool_call_id: r for r in executed}
            by_id.update(cache_hits)
            all_results: dict[str, ToolExecutionResult] = {}
            all_results.update(repeated_results)
            all_results.update(by_id)

            detection = None
            for tc in tool_calls:
                call_id = str(tc.get("id", ""))
                res = all_results.get(call_id)
                if res is None:
                    res = ToolExecutionResult.blocked(
                        tool_call_id=call_id,
                        tool_name=str(tc.get("name", "")),
                        error_code="execution_skipped",
                        message="[工具未执行] 本次调用未执行",
                    )
                self._emit_tool_event(res, round_no)
                self._settle_tool_result(res, call_id, tc)
                messages.append(ToolMessage(content=res.message, tool_call_id=call_id))
                if res.args_hash:
                    detection = self.detector.observe_action(
                        tool_name=res.tool_name,
                        args_hash=res.args_hash,
                        result_hash=res.result_hash,
                        new_evidence_count=len(res.source_ids),
                        error_code=res.error_code,
                    )
                    if detection is not None:
                        break

            self._emit(EventType.ROUND_END, phase="execute", round_no=round_no)

            # LoopDetector：首次命中 replan，再次命中 stop
            if detection is not None:
                self._emit(
                    EventType.DEGRADE,
                    phase="replan",
                    round_no=round_no,
                    error_code=f"loop:{detection.signal.value}",
                )
                if detection.should_stop:
                    loop_stopped = True
                    finalize_needed = True
                    finalize_reason = f"loop_stopped:{detection.signal.value}"
                    break
                self._enter_phase(LoopPhase.REPLAN)
                messages.append(
                    HumanMessage(
                        content=(
                            f"[检测到无进展] {detection.reason}。"
                            "请改变策略或直接回答，不要重复相同操作。"
                        )
                    )
                )
                self.detector.reset()

        # ── 终态 ──────────────────────────────────────────
        if cancelled:
            self._emit(EventType.CANCEL, phase="stop", error_code="deadline_expired")
            return self._result(
                status=LoopStatus.CANCELLED,
                answer="",
                degraded=True,
                degrade_reason="cancelled",
            )

        self._enter_phase(LoopPhase.FINALIZE)
        status = (
            LoopStatus.BUDGET_EXCEEDED
            if finalize_needed and not loop_stopped
            else LoopStatus.DEGRADED
        )
        return await self._finalize(
            messages=messages,
            bound_llm=bound_llm,
            status=status,
            degrade_reason=finalize_reason or "loop_terminated",
        )

    # ── FINALIZE ────────────────────────────────────────────

    async def _finalize(
        self,
        *,
        messages: list[BaseMessage],
        bound_llm: Any,
        status: LoopStatus,
        degrade_reason: str,
    ) -> LoopResult:
        """finalization：必须有预留额度，禁止工具调用；无预留不调用模型。"""
        round_no = self.contract_usage.rounds
        self._enter_phase(LoopPhase.FINALIZE)
        self._emit(
            EventType.FINALIZATION,
            phase="finalize",
            round_no=round_no,
            error_code=degrade_reason,
        )

        reservation = self.budget_manager.reserve_finalization_nowait(model_id=self._model_name())
        if reservation is None:
            # 无 finalization 预留预算：返回结构化 budget_exhausted，不得额外调用模型
            self._emit(EventType.LOOP_END, phase="stop", round_no=round_no)
            return self._result(
                status=LoopStatus.BUDGET_EXCEEDED,
                answer="",
                degraded=True,
                degrade_reason="budget_exhausted_no_finalization",
            )

        try:
            final_llm = self.llm_wrapper.llm.bind_tools([], tool_choice="none")
            final_retry_state = RetryState()
            response = await self.llm_wrapper.invoke_agent_step(
                bound_llm=final_llm,
                messages=messages,
                run_context=self.run_context,
                loop_round=round_no,
                max_attempts=max(1, self.budget_plan.max_retries + 1),
                retry_state_out=final_retry_state,
            )
            final_retries = final_retry_state.total_attempts
            if final_retries:
                self.budget_manager.record_retry(tokens_used=0)
            answer = (
                response.content if isinstance(response.content, str) else str(response.content)
            )
            answer = answer.strip()
            in_t, out_t, cached_t, estimated = self.llm_wrapper._resolve_usage(messages, response)
            self.budget_manager.settle_llm(
                reservation,
                input_tokens=in_t,
                output_tokens=out_t,
                cached_input_tokens=cached_t,
                usage_estimated=estimated,
                reason_code="ok",
                attribution="primary",
            )
            self.contract_usage.input_tokens += in_t
            self.contract_usage.output_tokens += out_t
            self.contract_usage.cost_usd = self.budget_manager.usage.cost_usd
        except Exception:
            logger.exception("[%s] finalization failed", self.run_context.trace_id)
            self.budget_manager.settle_llm(
                reservation,
                input_tokens=0,
                output_tokens=0,
                usage_estimated=True,
                reason_code="failed",
                attribution="primary",
            )
            answer = "基于当前上下文，暂无法提供完整回答。"

        self._emit(EventType.LOOP_END, phase="stop", round_no=round_no)
        return self._result(
            status=status,
            answer=answer,
            degraded=True,
            degrade_reason=degrade_reason,
        )

    # ── VALIDATE ────────────────────────────────────────────

    def _validate_output(
        self,
        answer: str,
        *,
        acceptance_criteria: list[str] | None,
        output_schema: type[Any] | None,
        validate_answer: Callable[[str], tuple[bool, str]] | None,
    ) -> tuple[bool, str]:
        """VALIDATE：输出必须非空；有 schema/criteria/validator 时进一步验证。"""
        if not answer or not answer.strip():
            return False, "answer_empty"
        if validate_answer is not None:
            try:
                ok, reason = validate_answer(answer)
                return bool(ok), str(reason or "validator_rejected")
            except Exception:
                return False, "validator_error"
        if output_schema is not None:
            try:
                if hasattr(output_schema, "model_validate_json"):
                    output_schema.model_validate_json(answer)
                elif hasattr(output_schema, "model_validate"):
                    output_schema.model_validate(answer)
            except Exception:
                return False, "schema_validation_failed"
        if acceptance_criteria:
            # 保守检查：仅当答案明显不包含任何验收关键词时视为不满足
            lowered = answer.lower()
            if not any(c.strip().lower() in lowered for c in acceptance_criteria if c.strip()):
                return False, "acceptance_criteria_unmet"
        return True, ""

    # ── 结果与事件 ──────────────────────────────────────────

    def _result(
        self,
        *,
        status: LoopStatus,
        answer: str = "",
        degraded: bool = False,
        degrade_reason: str = "",
    ) -> LoopResult:
        return LoopResult(
            status=status,
            answer=answer,
            rounds=self.contract_usage.rounds,
            usage=self.contract_usage,
            references=self.references,
            events=self._events,
            degraded=degraded,
            degrade_reason=degrade_reason,
            tool_names_used=self.tool_names_used,
        )

    def _enter_phase(self, phase: LoopPhase) -> None:
        if getattr(self, "_current_phase", None) != phase:
            self._current_phase = phase
            self.phase = phase

    def _emit(
        self,
        event_type: EventType,
        *,
        phase: LoopPhase | None = None,
        round_no: int = 0,
        tool_name: str = "",
        args_hash: str = "",
        result_hash: str = "",
        error_code: str = "",
        duration_ms: int = 0,
    ) -> None:
        """追加内存事件并登记落库（fire-and-forget）。"""
        event = AgentEvent(
            type=event_type,
            sequence=self._seq,
            run_id=self.run_context.run_id,
            trace_id=self.run_context.trace_id,
            timestamp=datetime.now(UTC),
            tool_name=tool_name,
            tool_args_hash=args_hash,
            tool_result_hash=result_hash,
            error_code=error_code,
            duration_ms=duration_ms,
            round_no=round_no,
        )
        self._events.append(event)
        self._seq += 1

        current = phase or self._current_phase
        event_name = _EVENT_TYPE_MAP.get(event_type, "loop_event")
        self._pending_store.append(
            {
                "run_id": self.run_context.run_id,
                "event_type": event_name,
                "trace_id": self.run_context.trace_id,
                "phase": _PHASE_MAP.get(current, "execute"),
                "step_id": f"r{round_no}" if round_no else "",
                "status": event_type.value,
                "model_id": self._route_decision or self._model_name(),
                "tool_name": tool_name,
                "input_hash": args_hash,
                "result_hash": result_hash,
                "duration_ms": duration_ms,
                "reason_code": error_code,
            }
        )

    def _emit_tool_event(self, res: ToolExecutionResult, round_no: int) -> None:
        if res.ok:
            self._emit(
                EventType.TOOL_FINISHED,
                phase="execute",
                round_no=round_no,
                tool_name=res.tool_name,
                args_hash=res.args_hash,
                result_hash=res.result_hash,
                duration_ms=res.duration_ms,
            )
            return
        if res.error_code in (
            "tool_not_found",
            "repeated_action",
            "budget_exhausted",
            "execution_skipped",
            "validation_failed",
            "contract_error",
        ) or res.error_code.startswith("policy_denied"):
            self._emit(
                EventType.TOOL_BLOCKED,
                phase="policy",
                round_no=round_no,
                tool_name=res.tool_name,
                args_hash=res.args_hash,
                error_code=res.error_code,
            )
        else:
            self._emit(
                EventType.TOOL_FAILED,
                phase="execute",
                round_no=round_no,
                tool_name=res.tool_name,
                args_hash=res.args_hash,
                error_code=res.error_code,
                duration_ms=res.duration_ms,
            )

    def _settle_tool_result(
        self,
        res: ToolExecutionResult,
        call_id: str,
        tc: dict[str, Any],
    ) -> None:
        """工具预算结算/释放 + Token 优化（压缩、缓存）+ 用量/来源汇总。"""
        rsv = self.executor.take_reservation(call_id)
        if rsv is not None:
            if res.ok:
                self.budget_manager.record_tool_call()
                self.budget_manager.release(rsv)
            else:
                self.budget_manager.usage.record_failed_tool(max(0, len(res.message) // 4))
                self.budget_manager.release(rsv)
        if res.ok:
            if res.tool_name not in self.tool_names_used:
                self.tool_names_used.append(res.tool_name)
            for sid in res.source_ids:
                if sid not in self.references:
                    self.references.append(sid)
            # 压缩（去重/截断）
            if res.result_hash:
                summary = self.compressor.compress_tool_result(
                    content=res.message,
                    result_hash=res.result_hash,
                    source_ids=res.source_ids,
                )
                res.message = summary.content
            # 写入缓存（仅成功结果，按权限边界）
            if self.tool_result_cache is not None:
                self.tool_result_cache.set(
                    tenant_id=self.run_context.tenant_id or "",
                    user_id=self.run_context.user_id,
                    tool_name=res.tool_name,
                    args=tc.get("args", {}) or {},
                    content=res.message,
                    source_ids=res.source_ids,
                    result_hash=res.result_hash,
                )

    # ── 模型路由（3.3：记录 route decision，不切换实例） ──

    def _record_route_decision(self, messages: list[BaseMessage]) -> None:
        if self.model_router is None:
            return
        try:
            from agent.model_router import RouteRequest, TaskType

            context_chars = sum(len(str(getattr(m, "content", "")) or "") for m in messages)
            remaining_input = max(
                0, self.budget_plan.max_input_tokens - self.budget_manager.usage.input_tokens
            )
            remaining_output = max(
                0, self.budget_plan.max_output_tokens - self.budget_manager.usage.output_tokens
            )
            decision = self.model_router.route(
                RouteRequest(
                    task_type=TaskType.DECIDE,
                    context_chars=context_chars,
                    remaining_input_tokens=remaining_input,
                    remaining_output_tokens=remaining_output,
                    remaining_cost_usd=max(
                        0.0, self.budget_plan.max_cost_usd - self.budget_manager.usage.cost_usd
                    ),
                )
            )
            self._route_decision = decision.model
            if decision.degraded:
                logger.info(
                    "[%s] route degraded to %s reason=%s",
                    self.run_context.trace_id,
                    decision.model,
                    decision.reason_code,
                )
        except Exception:
            logger.warning("[%s] route decision failed, keep default", self.run_context.trace_id)

    # ── 事件落库（可选，失败不影响 Loop） ──────────────────

    def _on_budget_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """BudgetManager 事件回调 -> EventEnvelope（同步登记，统一 flush）。"""
        if event_type not in (
            "budget_reserved",
            "budget_settled",
            "budget_released",
            "budget_warning",
            "budget_denied",
            "budget_exhausted",
        ):
            return
        self._pending_store.append(
            {
                "run_id": self.run_context.run_id,
                "event_type": event_type,
                "trace_id": self.run_context.trace_id,
                "phase": _PHASE_MAP.get(self._current_phase, "budget"),
                "status": event_type,
                "reason_code": str(payload.get("reason", "")),
                "extra": payload,
            }
        )

    def _on_tool_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """ToolExecutor 事件回调 -> EventEnvelope（tool_started 等）。"""
        if event_type not in ("tool_started", "tool_finished", "tool_failed"):
            return
        self._pending_store.append(
            {
                "run_id": self.run_context.run_id,
                "event_type": event_type,
                "trace_id": self.run_context.trace_id,
                "phase": "execute",
                "step_id": str(payload.get("step_id", "")),
                "tool_name": str(payload.get("tool_name", "")),
                "input_hash": str(payload.get("input_hash", "")),
                "result_hash": str(payload.get("result_hash", "")),
                "duration_ms": int(payload.get("duration_ms", 0)),
                "reason_code": str(payload.get("error_code", "")),
                "extra": {"source_ids": payload.get("source_ids", [])},
            }
        )

    async def _flush_events(self) -> None:
        """将登记的事件批量写入 AgentEventStore（失败仅记日志）。"""
        if self.event_store is None or not self._pending_store:
            return
        pending, self._pending_store = self._pending_store, []
        for payload in pending:
            await self.event_store.emit(**payload)

    # ── 工具 ──────────────────────────────────────────────

    @staticmethod
    def _args_hash(args: dict[str, Any]) -> str:
        combined = str(sorted(args.items())) if isinstance(args, dict) else str(args)
        return f"sha256:{hashlib.sha256(combined.encode('utf-8')).hexdigest()[:16]}"

    def _model_name(self) -> str:
        return str(
            getattr(self.llm_wrapper.llm, "model_name", None)
            or getattr(self.llm_wrapper.llm, "model", None)
            or "unknown"
        )
