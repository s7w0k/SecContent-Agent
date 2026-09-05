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
        # ── Final Closure（EPIC-A / EPIC-B）不破坏既有可运行环境 ─
        run_store: Any | None = None,
        skill_snapshot_hash: str = "",
        wiki_version: str = "",
        task_id: str = "",
        reviewer: Any | None = None,
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
        # Durable Resume（EPIC-A §8 / §9）：nil-safe，None 时跳过持久化与幂等注入。
        self.run_store = run_store
        self.skill_snapshot_hash = skill_snapshot_hash
        self.wiki_version = wiki_version
        self.task_id = task_id
        # Reviewer 主链 Hook（EPIC-B §21 / §52）：默认 None 保持主链不变。
        self.reviewer = reviewer

    # ── 公开入口 ──────────────────────────────────────────

    async def run(
        self,
        *,
        goal: str,
        user_id: str,
        tenant_id: str,
        trace_id: str = "",
        task_id: str = "",
    ) -> OrchestratorState:
        if task_id:
            self.task_id = task_id
        state = OrchestratorState(run_id=f"run-{uuid.uuid4().hex[:12]}", goal=goal)
        choice = await self._resolve_intent(goal, state)
        state.status = "PLANNING"
        record_created = False

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
                await self._persist_terminal(state)
                return state

            state.plan_ref = plan.plan_id
            state.status = "RUNNING"
            # Durable Resume（EPIC-A §8）：首次建 record；replan 时仅刷新 plan 元数据。
            if not record_created:
                await self._create_record(state, choice, plan, user_id, tenant_id)
                record_created = True
            elif self.run_store is not None:
                await self._refresh_record_plan(state, plan)
            outcome, _reason = await self._execute_plan(
                plan,
                state,
                user_id=user_id,
                tenant_id=tenant_id,
                trace_id=trace_id,
            )
            if outcome in ("ok", "failed"):
                await self._persist_terminal(state)
                return state

            # outcome == "replan"：必选步骤失败，受控重规划（bounded）。
            if state.replan_count >= self.budget.max_replans:
                state.status = "FAILED"
                await self._persist_terminal(state)
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

        # Resume（EPIC-A §11）：已完成的 step 从 pending 移除并复用其 Artifact，绝不重跑。
        for step in list(pending.values()):
            if step.step_id in state.completed_steps:
                ref = state.artifact_refs.get(step.step_id, "")
                completed[step.step_id] = SkillResult.succeeded(
                    step.skill_name, artifact_refs=([ref] if ref else [])
                )
                pending.pop(step.step_id)

        while pending:
            ready = [s for s in pending.values() if all(dep in completed for dep in s.depends_on)]
            if not ready:
                # 依赖无法满足：剩余必选步骤标记失败并整体失败。
                for s in pending.values():
                    if self.planner.is_required(plan.intent, s.step_id):
                        state.failed_steps.append(s.step_id)
                        if self.run_store is not None:
                            await self.run_store.mark_step_failed(state.run_id, s.step_id)
                state.status = "FAILED"
                return "failed", "unresolved_dependency"

            for step in ready:
                pending.pop(step.step_id)
                if self._condition_holds(plan, state, step, completed):
                    if self.run_store is not None:
                        await self.run_store.mark_step_started(state.run_id, step.step_id)
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
                    if self.run_store is not None:
                        await self.run_store.mark_step_completed(
                            state.run_id, step.step_id, list(result.artifact_refs or [])
                        )
                    self._emit(
                        goal=plan.goal,
                        run_id=state.run_id,
                        event_type="step_completed",
                        step=step.step_id,
                        status=result.status,
                    )
                    # Reviewer 主链 Hook（EPIC-B §21 / §52）：Draft 步骤完成后进入审查。
                    if self.reviewer is not None:
                        await self._run_review(
                            plan,
                            state,
                            step,
                            user_id=user_id,
                            tenant_id=tenant_id,
                            trace_id=trace_id,
                        )
                        if state.status == "BLOCKED":
                            return "failed", "blocked_by_reviewer"
                else:
                    if self.planner.is_required(plan.intent, step.step_id):
                        state.failed_steps.append(step.step_id)
                        if self.run_store is not None:
                            await self.run_store.mark_step_failed(state.run_id, step.step_id)
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
            # 每个 Skill 调用注入幂等键（EPIC-A §9 / §13）：task+plan+step+snapshot。
            params={
                "idempotency_key": (
                    f"{self.task_id}-{plan.plan_id}-{step.step_id}-{self.skill_snapshot_hash}"
                )
            },
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

    # ── Durable Resume（EPIC-A §14 / §15）─────────────────

    async def resume(
        self,
        *,
        run_record: Any,
        user_id: str,
        tenant_id: str,
        trace_id: str = "",
    ) -> OrchestratorState:
        """从持久化的 ExecutionRunRecord 恢复执行（§10）。

        算法：load record → 重建 state（completed/artifacts/reviewer）→ 重建确定性
        SkillPlan → 跳过已完成的 step（_execute_plan 里 seed）→ 续跑首个未完成 step。
        """
        self.task_id = run_record.task_id or self.task_id
        intent: Intent = run_record.intent or self.default_intent
        state = OrchestratorState(
            run_id=run_record.run_id,
            goal=run_record.goal,
            plan_ref=run_record.plan_id,
            completed_steps=list(run_record.completed_steps),
            failed_steps=list(run_record.failed_steps),
            artifact_refs=dict(run_record.artifact_refs),
            reviewer_rounds=run_record.reviewer_rounds,
            status="RUNNING",
        )
        # Artifact Validation（EPIC-A §12）：存在性 / tenant 不匹配 → 显式 FAILED，绝不静默重跑。
        validation_error = await self._validate_resume_artifacts(state, tenant_id)
        if validation_error:
            state.failed_steps.extend(
                s for s in state.completed_steps if s not in state.failed_steps
            )
            state.status = "FAILED"
            self._emit(
                goal=state.goal,
                run_id=state.run_id,
                event_type="resume_rejected",
                reason=validation_error,
            )
            await self._persist_terminal(state)
            return state
        try:
            plan = self.planner.build_plan(
                OrchestratorChoice(intent=intent, desired_output=state.goal),
                run_id=state.run_id,
                goal=state.goal,
            )
        except (UnsupportedIntentError, SkillNotAllowedError) as exc:
            self._emit(
                goal=state.goal, run_id=state.run_id, event_type="plan_rejected", reason=str(exc)
            )
            state.status = "FAILED"
            await self._persist_terminal(state)
            return state

        state.plan_ref = plan.plan_id
        if self.run_store is not None:
            await self._refresh_record_plan(state, plan)
        try:
            await self._execute_plan(
                plan,
                state,
                user_id=user_id,
                tenant_id=tenant_id,
                trace_id=trace_id,
            )
        finally:
            await self._persist_terminal(state)
        return state

    # ── 持久化（EPIC-A §8 / §9）───────────────────────────

    @staticmethod
    def _parse_resume_ref(ref: str) -> tuple[str, str, int] | None:
        """把 "Type:<id>@<version>" 解析为 (type, id, version)；非法返回 None。"""
        if ":" not in ref or "@" not in ref:
            return None
        type_part, version_part = ref.rsplit("@", 1)
        try:
            version = int(version_part)
        except ValueError:
            return None
        return type_part.split(":", 1)[0], type_part.split(":", 1)[1], version

    async def _validate_resume_artifacts(self, state: OrchestratorState, tenant_id: str) -> str:
        """Resume 前校验已完成 Artifact 存在且租户匹配（EPIC-A §12）。失败返回原因。"""
        store = getattr(self.skill_runtime, "artifact_store", None)
        if store is None or not state.artifact_refs:
            return ""
        for step_id, ref in state.artifact_refs.items():
            parsed = self._parse_resume_ref(ref)
            if parsed is None:
                return f"invalid_artifact_ref:{step_id}"
            art_type, artifact_id, version = parsed
            try:
                record = await store.get_record(
                    artifact_id=artifact_id, artifact_type=art_type, version=version
                )
            except Exception:  # 产物缺失（或 store 不支持）按缺失处理
                return f"missing_artifact:{step_id}"
            if str(record.get("tenant_id") or "") != str(tenant_id or ""):
                return f"wrong_tenant_artifact:{step_id}"
        return ""

    async def _create_record(
        self,
        state: OrchestratorState,
        choice: OrchestratorChoice,
        plan: SkillPlan,
        user_id: str,
        tenant_id: str,
    ) -> None:
        if self.run_store is None:
            return
        from agent.execution.run_store import ExecutionRunRecord

        record = ExecutionRunRecord(
            run_id=state.run_id,
            task_id=self.task_id,
            execution_engine="skill_planned",
            execution_mode="skill_planned",
            user_id=user_id,
            tenant_id=tenant_id,
            goal=state.goal,
            intent=choice.intent,
            plan_id=plan.plan_id,
            plan_version=plan.planner_version,
            skill_snapshot_hash=self.skill_snapshot_hash or plan.skill_snapshot_hash,
            wiki_version=self.wiki_version,
            status="RUNNING",
        )
        await self.run_store.create_run(record)

    async def _refresh_record_plan(self, state: OrchestratorState, plan: SkillPlan) -> None:
        """replan / resume 时同步最新的 plan 元数据，保留已完成 step 的持久化状态。"""
        record = await self.run_store.get_by_run_id(state.run_id)  # type: ignore[union-attr]
        if record is None:
            return
        record.plan_id = plan.plan_id
        record.plan_version = plan.planner_version
        record.status = "RUNNING"
        await self.run_store.create_run(record)  # type: ignore[union-attr]

    async def _persist_terminal(self, state: OrchestratorState) -> None:
        if self.run_store is None:
            return
        if state.status == "COMPLETED":
            await self.run_store.mark_completed(state.run_id)
        elif state.status == "BLOCKED":
            await self.run_store.mark_blocked(state.run_id)
        else:
            await self.run_store.mark_failed(state.run_id)

    async def _run_review(
        self,
        plan: SkillPlan,
        state: OrchestratorState,
        step: Any,
        *,
        user_id: str,
        tenant_id: str,
        trace_id: str,
    ) -> None:
        try:
            await self.reviewer.after_draft(  # type: ignore[union-attr]
                plan=plan,
                state=state,
                step=step,
                skill_runtime=self.skill_runtime,
                scopes=self.scopes,
                user_id=user_id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                task_id=self.task_id,
                budget=self.budget,
            )
        except Exception:
            self._emit(
                goal=plan.goal, run_id=state.run_id, event_type="review_error", step=step.step_id
            )

    def _emit(self, goal: str, run_id: str, event_type: str, **fields: Any) -> None:
        if self.trace_emitter is None:
            return
        with suppress(Exception):  # Trace 失败不影响主流程
            self.trace_emitter(event_type=f"orchestrator.{event_type}", run_id=run_id, **fields)


__all__ = ["ConditionChecker", "IntentResolver", "OrchestratorAgent"]
