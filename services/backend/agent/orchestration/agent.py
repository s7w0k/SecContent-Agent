"""OrchestratorAgent - 决策循环 + SkillPlan 执行（计划 §32 / §34 / §54）。

设计要点：
  - 规划单位是 Skill Step 而非 Worker Step（§27 / §66）。
  - LLM 只输出 OrchestratorChoice，SkillPlan 由 Planner 服务端构建（§30）。
  - 依赖感知执行；required 失败 → replan（受 max_replans 限制）；optional 失败/条件不满足 → 跳过。
  - 状态持久化在 OrchestratorState（run_id 维度）。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from agent.orchestration.contracts import (
    Intent,
    OrchestratorBudget,
    OrchestratorChoice,
    OrchestratorState,
    SkillPlan,
)
from agent.orchestration.planner import (
    OrchestratorPlanner,
    SkillNotAllowedError,
    UnsupportedIntentError,
)
from agent.skills.contracts import SkillRequest, SkillResult
from agent.skills.runtime import SkillRuntime

# 根据 goal + 当前 state 产出受约束的 OrchestratorChoice（可为同步或异步）。
IntentResolver = Callable[
    [str, OrchestratorState], OrchestratorChoice | Awaitable[OrchestratorChoice]
]
# 条件求值器：返回 False 则跳过可选步骤（默认 None = 恒真）。
ConditionChecker = Callable[[OrchestratorState, str, dict[str, SkillResult]], bool]


class OrchestratorAgent:
    def __init__(
        self,
        *,
        skill_runtime: SkillRuntime,
        planner: OrchestratorPlanner | None = None,
        intent_resolver: IntentResolver | None = None,
        budget: OrchestratorBudget | None = None,
        scopes: frozenset[str] | None = None,
        trace_emitter: Any | None = None,
        default_intent: Intent = "full_workflow",
        condition_checker: ConditionChecker | None = None,
    ) -> None:
        self.skill_runtime = skill_runtime
        registry_names = skill_runtime.registry.names()
        self.planner = planner or OrchestratorPlanner(known_skills=registry_names)
        self.intent_resolver = intent_resolver
        self.budget = budget or OrchestratorBudget()
        self.scopes = frozenset(scopes or ())
        self.trace_emitter = trace_emitter
        self.default_intent = default_intent
        self.condition_checker = condition_checker

    # ── 公开入口 ──────────────────────────────────────────

    async def run(
        self,
        *,
        goal: str,
        user_id: str,
        tenant_id: str,
        trace_id: str = "",
    ) -> OrchestratorState:
        state = OrchestratorState(run_id=f"run-{uuid.uuid4().hex[:12]}", goal=goal)
        choice = await self._resolve_intent(goal, state)
        state.status = "PLANNING"

        while True:
            try:
                plan = self.planner.build_plan(choice, run_id=state.run_id, goal=goal)
            except (UnsupportedIntentError, SkillNotAllowedError) as exc:
                # LLM 被白名单/注册表拒绝 → 立即失败，无静默回退。
                self._emit(
                    goal=goal,
                    run_id=state.run_id,
                    event_type="plan_rejected",
                    reason=str(exc),
                )
                state.status = "FAILED"
                return state

            state.plan_ref = plan.plan_id
            state.status = "RUNNING"
            outcome, _reason = await self._execute_plan(
                plan,
                state,
                user_id=user_id,
                tenant_id=tenant_id,
                trace_id=trace_id,
            )
            if outcome in ("ok", "failed"):
                return state

            # outcome == "replan"：必选步骤失败，受控重规划（bounded）。
            if state.replan_count >= self.budget.max_replans:
                state.status = "FAILED"
                return state
            state.replan_count += 1
            choice = await self._resolve_intent(goal, state)

    # ── 决策 ──────────────────────────────────────────────

    async def _resolve_intent(self, goal: str, state: OrchestratorState) -> OrchestratorChoice:
        if self.intent_resolver is None:
            return OrchestratorChoice(intent=self.default_intent, desired_output=goal)
        choice = self.intent_resolver(goal, state)
        if isinstance(choice, Awaitable):
            choice = await choice
        return choice

    # ── 执行 ──────────────────────────────────────────────

    async def _execute_plan(
        self,
        plan: SkillPlan,
        state: OrchestratorState,
        *,
        user_id: str,
        tenant_id: str,
        trace_id: str,
    ) -> tuple[str, str]:
        pending: dict[str, Any] = {step.step_id: step for step in plan.steps}
        completed: dict[str, SkillResult] = {}
        skill_calls = 0

        while pending:
            ready = [s for s in pending.values() if all(dep in completed for dep in s.depends_on)]
            if not ready:
                # 依赖无法满足：剩余必选步骤标记失败并整体失败。
                for s in pending.values():
                    if self.planner.is_required(plan.intent, s.step_id):
                        state.failed_steps.append(s.step_id)
                state.status = "FAILED"
                return "failed", "unresolved_dependency"

            for step in ready:
                pending.pop(step.step_id)
                if self._condition_holds(plan, state, step, completed):
                    result = await self._execute_skill(
                        plan,
                        state,
                        step,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        trace_id=trace_id,
                    )
                    skill_calls += 1
                else:
                    # 可选步骤条件不满足：跳过，不执行。
                    self._emit(
                        goal=plan.goal,
                        run_id=state.run_id,
                        event_type="step_skipped",
                        step=step.step_id,
                        skill=step.skill_name,
                        reason="condition_not_met",
                    )
                    continue

                if result.status in ("SUCCEEDED", "PARTIAL"):
                    completed[step.step_id] = result
                    state.completed_steps.append(step.step_id)
                    if result.artifact_refs:
                        state.artifact_refs[step.step_id] = result.artifact_refs[0]
                    self._emit(
                        goal=plan.goal,
                        run_id=state.run_id,
                        event_type="step_completed",
                        step=step.step_id,
                        status=result.status,
                    )
                else:
                    if self.planner.is_required(plan.intent, step.step_id):
                        state.failed_steps.append(step.step_id)
                        return "replan", f"required_skill_failed:{step.skill_name}"
                    self._emit(
                        goal=plan.goal,
                        run_id=state.run_id,
                        event_type="step_skipped",
                        step=step.step_id,
                        skill=step.skill_name,
                        reason=str(result.status),
                    )

                if skill_calls >= self.budget.max_skill_calls:
                    state.status = "FAILED"
                    return "failed", "skill_budget_exceeded"

        state.status = "COMPLETED"
        return "ok", ""

    async def _execute_skill(
        self,
        plan: SkillPlan,
        state: OrchestratorState,
        step: Any,
        *,
        user_id: str,
        tenant_id: str,
        trace_id: str,
    ) -> SkillResult:
        request = SkillRequest(
            skill_name=step.skill_name,
            run_id=state.run_id,
            user_id=user_id,
            tenant_id=tenant_id,
            trace_id=trace_id or state.run_id,
            input_refs=self._step_input_refs(state, step),
            params={},
        )
        try:
            return await self.skill_runtime.execute(request, scopes=self.scopes)
        except Exception as exc:  # 防御：Runtime 已有局部异常映射，这里只兜底
            return SkillResult.failed(step.skill_name, "orchestrator_error", str(exc))

    def _step_input_refs(self, state: OrchestratorState, step: Any) -> dict[str, str]:
        """把前置步骤产出作为 ArtifactRef 传入（§18 / §46，不传大文本）。"""
        refs: dict[str, str] = {}
        for dep in step.depends_on:
            if dep in state.artifact_refs:
                refs[dep] = state.artifact_refs[dep]
        return refs

    def _condition_holds(
        self,
        plan: SkillPlan,
        state: OrchestratorState,
        step: Any,
        completed: dict[str, SkillResult],
    ) -> bool:
        if not step.condition:
            return True
        if self.planner.is_required(plan.intent, step.step_id):
            return True  # 必选步骤不因条件跳过
        if self.condition_checker is None:
            return True  # 默认放行
        try:
            return bool(self.condition_checker(state, step.condition, completed))
        except Exception:  # 求值异常时不阻断可选（视为跳过）
            return False

    def _emit(self, goal: str, run_id: str, event_type: str, **fields: Any) -> None:
        if self.trace_emitter is None:
            return
        with suppress(Exception):  # Trace 失败不影响主流程
            self.trace_emitter(event_type=f"orchestrator.{event_type}", run_id=run_id, **fields)


__all__ = ["ConditionChecker", "IntentResolver", "OrchestratorAgent"]
