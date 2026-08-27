"""OrchestrationRuntime - Orchestrator Agent 的门面（计划 §32 / §66 / §85）。

把 Planner + Agent + SkillRuntime 装配为一个 run(goal) 入口。
"""

from __future__ import annotations

from typing import Any

from agent.orchestration.agent import ConditionChecker, IntentResolver, OrchestratorAgent
from agent.orchestration.contracts import (
    Intent,
    OrchestratorBudget,
    OrchestratorChoice,
    OrchestratorState,
)
from agent.orchestration.planner import OrchestratorPlanner
from agent.skills.runtime import SkillRuntime


class OrchestrationRuntime:
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
        # ── Final Closure（EPIC-A / EPIC-B）─────────────────
        run_store: Any | None = None,
        skill_snapshot_hash: str = "",
        wiki_version: str = "",
        task_id: str = "",
        reviewer: Any | None = None,
    ) -> None:
        self.agent = OrchestratorAgent(
            skill_runtime=skill_runtime,
            planner=planner,
            intent_resolver=intent_resolver,
            budget=budget,
            scopes=scopes,
            trace_emitter=trace_emitter,
            default_intent=default_intent,
            condition_checker=condition_checker,
            run_store=run_store,
            skill_snapshot_hash=skill_snapshot_hash,
            wiki_version=wiki_version,
            task_id=task_id,
            reviewer=reviewer,
        )
        self.run_store = run_store
        self.skill_snapshot_hash = skill_snapshot_hash
        self.wiki_version = wiki_version
        self.task_id = task_id
        self.reviewer = reviewer

    async def run(
        self,
        *,
        goal: str,
        user_id: str,
        tenant_id: str,
        trace_id: str = "",
        task_id: str = "",
    ) -> OrchestratorState:
        return await self.agent.run(
            goal=goal,
            user_id=user_id,
            tenant_id=tenant_id,
            trace_id=trace_id,
            task_id=task_id,
        )

    async def resume(
        self,
        *,
        run_record: Any,
        user_id: str,
        tenant_id: str,
        trace_id: str = "",
    ) -> OrchestratorState:
        return await self.agent.resume(
            run_record=run_record,
            user_id=user_id,
            tenant_id=tenant_id,
            trace_id=trace_id,
        )

    def choose(self, intent: Intent, **kw: Any) -> OrchestratorChoice:
        """便捷构造受约束的 OrchestratorChoice。"""
        return OrchestratorChoice(intent=intent, **kw)


__all__ = ["OrchestrationRuntime"]
