"""Adapters that run a validated Plan v2 through the existing AgentRuntime state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.agent_runtime import PlannedAction
from agent.business_tools.contracts import BusinessToolRegistry, ToolRequestContext
from agent.business_tools.execution import (
    BusinessToolAdapterKind,
    BusinessToolExecutor,
)
from agent.contracts.task import SlotState
from agent.goal_validator import GoalValidationResult
from agent.harness.tool_harness import SideEffectLevel
from agent.observation import NormalizedObservation, ObservationNormalizer
from agent.policy_engine import PolicyAction, PolicyEngine, PolicyRule, RiskLevel
from agent.production_plan import ArgumentBinding, BindingSource, Plan
from agent.rule_planner import RulePlannerV1
from agent.runtime_state import RuntimeState, RuntimeStatus
from agent.workflow_validator import StepValidator, WorkflowDecision, WorkflowGoalValidator


def build_business_policy(registry: BusinessToolRegistry) -> PolicyEngine:
    rules: dict[str, PolicyRule] = {}
    for name in registry.names():
        contract = registry.get(name)
        writes = contract.side_effect_level != SideEffectLevel.L1
        approval = name == "save_draft_version"
        rules[name] = PolicyRule(
            tool_name=name,
            risk_level=RiskLevel.L2 if approval else (RiskLevel.L1 if writes else RiskLevel.L0),
            default_action=PolicyAction.REQUIRE_APPROVAL if approval else PolicyAction.ALLOW,
            has_side_effect=writes,
            allowed_args=frozenset(contract.args_schema.model_fields),
        )
    return PolicyEngine(rules)


def _path(value: Any, path: str) -> Any:
    if not path:
        return value
    current: list[Any] = [value]
    for part in path.split("."):
        next_values: list[Any] = []
        for item in current:
            if part == "*":
                if isinstance(item, list):
                    next_values.extend(item)
                continue
            if isinstance(item, dict):
                child = item.get(part)
            elif isinstance(item, list) and part.isdigit():
                index = int(part)
                child = item[index] if index < len(item) else None
            else:
                child = getattr(item, part, None)
            if child is not None:
                next_values.append(child)
        current = next_values
    if not current:
        return None
    return current[0] if len(current) == 1 else current


class ProductionActionPlanner:
    """Lazy Plan v2 builder and deterministic next-action selector."""

    adapter_type = "rule-first-v1"

    def __init__(self, planner: RulePlannerV1):
        self.planner = planner
        self.plan: Plan | None = None
        self.observations: dict[str, NormalizedObservation] = {}
        self.pause_status: RuntimeStatus | None = None
        self.pause_reason = ""
        self.pause_reason_code = ""
        self.pending_questions: list[str] = []

    async def __call__(self, state: RuntimeState) -> PlannedAction | None:
        for step_id, raw in state.normalized_observations.items():
            if step_id not in self.observations:
                self.observations[step_id] = NormalizedObservation.model_validate(raw)
        if self.plan is None:
            if state.task_envelope is None:
                raise ValueError("production runtime requires TaskEnvelope")
            virtual = self._virtual_slots(state)
            outcome = await self.planner.plan(
                state.task_envelope,
                run_id=state.run_id,
                confirmed_slot_names=set(virtual),
            )
            self.plan = outcome.plan
        self.pause_status = None
        self.pending_questions = []
        # 用户"授权系统决定/继续"的开关：分类离题或产品无匹配时，若用户已授权则由系统继续
        auto_slot = state.slot_states.get("auto_select")
        auto_select = bool(
            auto_slot and auto_slot.status.value == "confirmed" and auto_slot.value
        )
        # ── 关卡 1：分类结果与用户主题不符/离题 -> 停下让用户决定 ──
        classify = self.observations.get("classify")
        if classify and not auto_select:
            cdata = classify.data or {}
            conflict = str(cdata.get("conflict") or "")
            eligible = bool(cdata.get("eligible", True))
            category = str(cdata.get("category") or "unknown")
            domain = str(cdata.get("security_domain") or "")
            domain_hint = f"，安全域：{domain}" if domain and domain != "未知" else ""
            if (not eligible) or conflict:
                self.pause_status = RuntimeStatus.WAITING_USER
                self.pause_reason_code = "category_mismatch"
                self.pause_reason = "article classification conflicts with or is off-topic for the user request"
                if conflict:
                    hint = f"分类结果与您要求的类别不符（{conflict}；六分类：{category}{domain_hint}）"
                else:
                    hint = f"该新闻与目标主题不相关（六分类：{category}{domain_hint}）"
                self.pending_questions = [
                    f"{hint}。请处理：①仍用这篇继续 ②换下一条候选新闻 ③取消本次任务"
                ]
                return None
        products = self.observations.get("products")
        product_slot = state.slot_states.get("product_ids")
        product_confirmed = bool(
            product_slot and product_slot.status.value == "confirmed" and product_slot.value
        )
        # ── 关卡 2：分类通过但无产品匹配 / 产品歧义 → 停下让用户决定 ──
        if products and not product_confirmed:
            outcome = products.data.get("outcome")
            if outcome == "no_related_product":
                self.pause_status = RuntimeStatus.WAITING_USER
                self.pause_reason_code = "product_none"
                self.pause_reason = "no product in the catalog matches this article"
                self.pending_questions = [
                    "没有匹配到相关安全产品。请处理：①仍用这篇按通用口径继续 ②换下一条候选新闻 ③取消本次任务"
                ]
                return None
            if outcome == "ambiguous":
                self.pause_status = RuntimeStatus.WAITING_USER
                self.pause_reason_code = "product_ambiguity"
                self.pause_reason = "multiple products have similar confidence"
                self.pending_questions = ["请选择要用于评分和写稿的产品。"]
                return None
        discovery = self.observations.get("discover")
        article_slot = state.slot_states.get("selected_article_ids")
        article_confirmed = bool(
            article_slot and article_slot.status.value == "confirmed" and article_slot.value
        )
        auto_slot = state.slot_states.get("auto_select")
        auto_select = bool(
            auto_slot and auto_slot.status.value == "confirmed" and auto_slot.value
        )
        if discovery and not article_confirmed:
            candidates = discovery.data.get("items") or discovery.data.get("articles") or []
            if not candidates:
                self.pause_status = RuntimeStatus.WAITING_USER
                self.pause_reason_code = "crawl_suggested"
                self.pause_reason = "no existing article matched the query"
                self.pending_questions = ["没有找到已有文章，是否按当前范围抓取新闻？"]
                return None
            if len(candidates) > 1 and not auto_select:
                self.pause_status = RuntimeStatus.WAITING_USER
                self.pause_reason_code = "article_selection_required"
                self.pause_reason = "multiple news candidates require selection"
                self.pending_questions = ["请选择一篇新闻，或明确授权系统选择第一名。"]
                return None
        return await self._select_next_action(state)

    async def _select_next_action(self, state: RuntimeState) -> PlannedAction | None:
        """确定性选步：按计划顺序返回下一个可执行步骤（SOP 兜底路径）。"""
        completed = set(state.completed_steps)
        failed = set(state.failed_steps)
        for step in self.plan.steps:
            if step.step_id in completed:
                continue
            if step.step_id in failed and state.usage.retries >= step.retry_policy.max_attempts:
                continue
            if not set(step.dependencies).issubset(completed):
                continue
            args = {
                name: self._resolve(binding, state)
                for name, binding in step.args_binding.items()
            }
            return PlannedAction(step_id=step.step_id, tool_name=step.tool, args=args)
        return None

    def _virtual_slots(self, state: RuntimeState) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if state.artifact_refs:
            artifact = state.artifact_refs[-1]
            result["draft_artifact"] = {
                "artifact_id": artifact.artifact_id,
                "version": artifact.version,
                "content_hash": artifact.content_hash,
                "status": "draft",
            }
            result["draft_version"] = artifact.version
        return result

    def _resolve(self, binding: ArgumentBinding, state: RuntimeState) -> Any:
        if binding.source == BindingSource.SERVER_VALUE:
            return binding.value
        if binding.source == BindingSource.CONFIRMED_SLOT:
            virtual = self._virtual_slots(state)
            if binding.key in virtual:
                value = virtual[binding.key]
            else:
                slot: SlotState | None = state.slot_states.get(binding.key)
                value = slot.value if slot else None
            resolved = _path(value, binding.path)
            return self._coerce_binding(binding, resolved)
        observation = self.observations.get(binding.step_id)
        resolved = _path(observation.data if observation else None, binding.path)
        return self._coerce_binding(binding, resolved)

    @staticmethod
    def _coerce_binding(binding: ArgumentBinding, value: Any) -> Any:
        if binding.key == "article" and isinstance(value, dict):
            return {
                key: value.get(key, "")
                for key in ("article_id", "source_ref", "content_hash")
            }
        return value


class ProductionBusinessExecutor:
    adapter_type = "business-tools"

    def __init__(
        self,
        executor: BusinessToolExecutor,
        action_planner: ProductionActionPlanner,
        *,
        adapter: BusinessToolAdapterKind | str = BusinessToolAdapterKind.PRODUCTION,
        normalizer: ObservationNormalizer | None = None,
        step_validator: StepValidator | None = None,
    ):
        self.executor = executor
        self.action_planner = action_planner
        self.adapter = adapter
        self.normalizer = normalizer or ObservationNormalizer()
        self.step_validator = step_validator or StepValidator()

    async def __call__(
        self, state: RuntimeState, action: PlannedAction, meta: dict[str, Any]
    ) -> dict[str, Any]:
        contract = self.executor.registry.get(action.tool_name)
        context = ToolRequestContext(
            user_id=state.user_id,
            tenant_id=state.tenant_id or state.user_id,
            scopes=frozenset(contract.required_scopes),
            run_id=state.run_id,
            turn_id=state.current_turn_id,
        )
        try:
            result = await self.executor.invoke(
                action.tool_name, action.args, context=context, adapter=self.adapter
            )
            observation = await self.normalizer.success(contract, result)
        except Exception as exc:
            observation = self.normalizer.failure(exc)
        self.action_planner.observations[action.step_id] = observation
        step = next(
            item for item in (self.action_planner.plan.steps if self.action_planner.plan else [])
            if item.step_id == action.step_id
        )
        decision = self.step_validator.validate(step, observation)
        accepted = decision.decision == WorkflowDecision.CONTINUE
        evidence = [
            {"kind": "tool_result", "note": ref[:200]}
            for ref in observation.evidence
        ]
        return {
            "ok": accepted,
            "data": observation.data,
            "evidence": evidence,
            "warnings": observation.warnings,
            "retryable": observation.retryable,
            "error_code": "" if accepted else decision.reason_code,
            "error": "" if accepted else decision.reason,
            "result_hash": observation.result_hash,
            "source_ids": observation.evidence[:100],
            "normalized_observation": observation.model_dump(mode="json"),
        }


class ProductionGoalAdapter:
    def __init__(self, action_planner: ProductionActionPlanner):
        self.action_planner = action_planner
        self.validator = WorkflowGoalValidator()

    def validate(
        self,
        state: RuntimeState,
        *,
        artifacts: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> GoalValidationResult:
        if self.action_planner.plan is None:
            return GoalValidationResult(
                status="incomplete", reason="production plan has not been built"
            )
        decision = self.validator.validate(
            self.action_planner.plan,
            self.action_planner.observations,
            approval_pending=any(
                item.status == "pending" for item in state.approval_state.pending_approvals
            ),
        )
        status = "complete" if decision.decision == WorkflowDecision.COMPLETE else "incomplete"
        if decision.decision == WorkflowDecision.WAIT_APPROVAL:
            status = "blocked"
        return GoalValidationResult(
            status=status,
            reason=decision.reason,
            validated_at=now or datetime.now(UTC),
        )
