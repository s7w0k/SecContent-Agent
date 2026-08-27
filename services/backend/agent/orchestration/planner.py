"""OrchestratorPlanner - 据 OrchestratorChoice 构建 SkillPlan（计划 §29 / §30 / §31）。

LLM 只提供一个受约束的 intent，服务端把白名单映射为 Skill Step 计划。
安全性由 policy 白名单 + 注册 Skill 名校验双重把关：LLM 无法自造 Skill。
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from agent.orchestration.contracts import OrchestratorChoice, SkillPlan, SkillPlanStep
from agent.orchestration.policy import INTENT_STEPS, authorize_skill, step_is_required


class OrchestratorPlanError(RuntimeError):
    """Orchestrator 计划层错误基类。"""


class UnsupportedIntentError(OrchestratorPlanError):
    """LLM 输出未知 intent（被拒绝，不静默降级）。"""

    def __init__(self, intent: str) -> None:
        self.intent = intent
        super().__init__(f"unsupported intent: {intent}")


class SkillNotAllowedError(OrchestratorPlanError):
    """intent 未授权 / 注册表中不存在该 Skill（LLM 无法自造）。"""

    def __init__(self, intent: str, skill_name: str) -> None:
        self.intent = intent
        self.skill_name = skill_name
        super().__init__(f"skill '{skill_name}' not allowed for intent '{intent}'")


class OrchestratorPlanner:
    """确定性 SkillPlan 构建器。

    known_skills 注入 ExecutableSkillRegistry 的已注册 Skill 名，用于 fail-closed：
    即便白名单写了某 Skill，只要未注册/未发布，也拒绝构卷（防止造出打不通的步骤）。
    """

    def __init__(self, *, known_skills: Collection[str] | None = None) -> None:
        self._known_skills = frozenset(known_skills) if known_skills is not None else frozenset()

    def build_plan(
        self,
        choice: OrchestratorChoice,
        *,
        run_id: str,
        goal: str = "",
    ) -> SkillPlan:
        specs = INTENT_STEPS.get(choice.intent)
        if not specs:
            raise UnsupportedIntentError(choice.intent)

        steps: list[SkillPlanStep] = []
        for i, spec in enumerate(specs, start=1):
            step_id = f"s{i}"
            if not authorize_skill(choice.intent, spec.skill_name):
                raise SkillNotAllowedError(choice.intent, spec.skill_name)
            if self._known_skills and spec.skill_name not in self._known_skills:
                raise SkillNotAllowedError(choice.intent, spec.skill_name)
            steps.append(
                SkillPlanStep(
                    step_id=step_id,
                    skill_name=spec.skill_name,
                    depends_on=list(spec.depends_on),
                    condition=spec.condition,
                    max_attempts=spec.max_attempts,
                )
            )

        return SkillPlan(
            plan_id=f"plan-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            goal=goal or choice.desired_output or choice.intent,
            intent=choice.intent,
            steps=steps,
        )

    def is_required(self, intent: str, step_id: str) -> bool:
        """策略层：该 step 是否必选（可选失败/条件不满足时可跳过）。"""
        return step_is_required(intent, step_id)


__all__ = [
    "OrchestratorPlanError",
    "OrchestratorPlanner",
    "SkillNotAllowedError",
    "UnsupportedIntentError",
]
