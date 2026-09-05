"""LLM 驱动的下一步决策器：把"按固定顺序执行计划"升级为"模型每轮决策"。

定位：
  - Plan v2 仍由规则规划器生成，作为能力边界与校验依据（依赖图、参数绑定、预算）；
  - 每轮把【任务目标 / 槽位状态 / 已完成步骤与观察摘要 / 可执行步骤】交给 LLM，
    由它决定：执行哪个步骤（call_step）、需要用户拍板（ask_user）、或判定收尾（finish）；
  - 分类/产品/选稿等硬关卡在父类 __call__ 中先于本决策强制生效，LLM 无法越过关卡；
  - LLM 失败、超时或输出非法时，回退父类确定性顺序（SOP 兜底），保证可用性。

这就是"决策层 agent 化"：执行、校验、预算、关卡仍是确定性护栏，
"下一步做什么"由模型基于观察自主判断。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from agent.production_runtime import ProductionActionPlanner
from agent.runtime_state import RuntimeState
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("backend.agent.llm_action_planner")

_DECISION_TIMEOUT_SECONDS = 20.0

_SYSTEM_PROMPT = """你是 PR 内容生产 Agent 的决策大脑，负责在每一轮决定下一步动作。

可选动作只有三种：
1. call_step：从「可执行步骤」中选择一个 step_id 执行；
2. ask_user：关键信息缺失或存在需要用户拍板的不确定性时，向用户提出一个自然、具体、口语化的中文问题；
3. finish：可执行步骤为空、或剩余步骤都无法再推进目标时，判定本轮工作收尾。

硬性规则：
- step_id 必须来自「可执行步骤」列表，不得编造，不得选择已完成步骤；
- 用户已确认的信息（如选定的文章、产品）不要再次询问；
- 观察结果中已明确的信息（如分类结论、产品匹配结果）优先直接利用，不要重复执行已完成的步骤；
- 问题必须一次只问一件事，并给出必要的候选项。

只输出 JSON：
{"decision": "call_step 或 ask_user 或 finish", "step_id": "", "question": "", "reason": ""}"""


class _PlannerDecision(BaseModel):
    decision: Literal["call_step", "ask_user", "finish"] = "finish"
    step_id: str = Field(default="", max_length=100)
    question: str = Field(default="", max_length=1000)
    reason: str = Field(default="", max_length=500)

    @field_validator("step_id", "question", "reason", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> str:
        return str(value or "").strip()


def _compact(value: Any, limit: int = 320) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


class LLMActionPlanner(ProductionActionPlanner):
    """在 ProductionActionPlanner 护栏之上，用 LLM 决定每轮下一步。"""

    adapter_type = "llm-select-v1"

    def __init__(
        self,
        planner: Any,
        *,
        llm_factory: Callable[[], Any],
        timeout_seconds: float = _DECISION_TIMEOUT_SECONDS,
    ):
        super().__init__(planner)
        self.llm_factory = llm_factory
        self.timeout_seconds = timeout_seconds
        self._wrapper: Any = None

    def _wrapper_or_none(self) -> Any:
        if self._wrapper is None:
            try:
                from agent.llm_wrapper import LLMWrapper

                self._wrapper = LLMWrapper(self.llm_factory(), None)
            except Exception as exc:  # LLM 不可用时由兜底路径接管
                logger.warning("planner LLM unavailable, fallback to rule order: %s", exc)
                self._wrapper = False
        return self._wrapper or None

    async def _select_next_action(self, state: RuntimeState):
        fallback = await super()._select_next_action(state)
        wrapper = self._wrapper_or_none()
        if wrapper is None:
            return fallback
        try:
            decision = await asyncio.wait_for(
                self._decide(wrapper, state),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            logger.warning("planner LLM decision failed, fallback to rule order: %s", exc)
            return fallback
        if decision is None:
            return fallback
        logger.info(
            "planner LLM decision=%s step_id=%s q=%s reason=%s",
            decision.decision,
            decision.step_id or "-",
            decision.question or "-",
            decision.reason or "-",
        )
        if decision.decision == "ask_user" and decision.question:
            self.pause_status = self._waiting_status()
            self.pause_reason_code = "llm_ask_user"
            self.pause_reason = decision.reason or "the planner needs user input to proceed"
            self.pending_questions = [decision.question]
            return None
        if decision.decision == "call_step":
            action = self._action_for(decision.step_id, state)
            if action is not None:
                return action
        # finish / 非法 step_id / 缺参数：交给确定性兜底（含自然收尾）
        return fallback

    # ── LLM 决策 ──────────────────────────────────────────────

    async def _decide(self, wrapper: Any, state: RuntimeState) -> _PlannerDecision | None:
        payload = await wrapper.invoke_structured(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=self._build_user_prompt(state),
            output_schema=_PlannerDecision,
            agent_type="llm_action_planner",
            user_id=state.user_id,
            trace_id=state.run_id,
            task_id=state.run_id,
        )
        data = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
        return _PlannerDecision.model_validate(data)

    def _build_user_prompt(self, state: RuntimeState) -> str:
        envelope = state.task_envelope
        slots: dict[str, Any] = {}
        if envelope is not None:
            for name in (
                "selected_article_ids",
                "product_ids",
                "category",
                "auto_select",
                "crawl_approved",
                "search_more",
                "constraints",
                "tone",
                "length",
            ):
                slot = envelope.slot_states().get(name)
                if slot is not None and slot.value not in (None, "", []):
                    slots[name] = slot.value
        completed = sorted(set(state.completed_steps))
        failed = sorted(set(state.failed_steps))
        observations = {
            step_id: _compact(observation.data)
            for step_id, observation in self.observations.items()
        }
        actionable = [
            {
                "step_id": step.step_id,
                "tool": step.tool,
                "goal": step.goal,
                "depends_on": step.dependencies,
            }
            for step in self._actionable_steps(state)
        ]
        context = {
            "task_goal": str(envelope.goal.value) if envelope is not None else "",
            "intent": str(envelope.intent.value) if envelope is not None else "",
            "confirmed_slots": slots,
            "completed_steps": completed,
            "failed_steps": failed,
            "observations": observations,
            "actionable_steps": actionable,
        }
        return "当前任务状态如下，请决定下一步：\n" + json.dumps(
            context, ensure_ascii=False, default=str
        )

    # ── 步骤可行性 ────────────────────────────────────────────

    def _actionable_steps(self, state: RuntimeState) -> list[Any]:
        if self.plan is None:
            return []
        completed = set(state.completed_steps)
        failed_exhausted = {
            step.step_id
            for step in self.plan.steps
            if step.step_id in set(state.failed_steps)
            and state.usage.retries >= step.retry_policy.max_attempts
        }
        result = []
        for step in self.plan.steps:
            if step.step_id in completed or step.step_id in failed_exhausted:
                continue
            if not set(step.dependencies).issubset(completed):
                continue
            result.append(step)
        return result

    def _action_for(self, step_id: str, state: RuntimeState):
        step = next(
            (item for item in self._actionable_steps(state) if item.step_id == step_id),
            None,
        )
        if step is None:
            return None
        args = {name: self._resolve(binding, state) for name, binding in step.args_binding.items()}
        from agent.agent_runtime import PlannedAction

        return PlannedAction(step_id=step.step_id, tool_name=step.tool, args=args)

    @staticmethod
    def _waiting_status():
        from agent.runtime_state import RuntimeStatus

        return RuntimeStatus.WAITING_USER
