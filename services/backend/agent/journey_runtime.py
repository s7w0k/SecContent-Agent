"""One production Runtime entry for complete conversational business journeys."""

from __future__ import annotations

from typing import Any

from agent.agent_runtime import AgentRuntime
from agent.business_tools.execution import BusinessToolAdapterKind, BusinessToolExecutor
from agent.contracts.task import TaskEnvelope
from agent.production_runtime import (
    ProductionActionPlanner,
    ProductionBusinessExecutor,
    ProductionGoalAdapter,
    build_business_policy,
)
from agent.rule_planner import RulePlannerV1
from agent.runtime_state import RunBudget, RuntimeState
from pydantic import BaseModel, Field


class FinalJourneyResult(BaseModel):
    run_id: str
    status: str
    reason_code: str
    article: dict[str, Any] = Field(default_factory=dict)
    classification: dict[str, Any] = Field(default_factory=dict)
    products: dict[str, Any] = Field(default_factory=dict)
    score: dict[str, Any] = Field(default_factory=dict)
    artifact: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)


class ProductionJourneyRunner:
    def __init__(
        self,
        executor: BusinessToolExecutor,
        *,
        adapter: BusinessToolAdapterKind | str = BusinessToolAdapterKind.PRODUCTION,
    ):
        self.executor = executor
        self.adapter = adapter

    async def run(
        self,
        task: TaskEnvelope,
        *,
        prior_state: RuntimeState | None = None,
        budget: RunBudget | None = None,
    ) -> tuple[RuntimeState, FinalJourneyResult]:
        action_planner = ProductionActionPlanner(RulePlannerV1(self.executor.registry))
        tool_executor = ProductionBusinessExecutor(
            self.executor, action_planner, adapter=self.adapter
        )
        runtime = AgentRuntime(
            planner=action_planner,
            executor=tool_executor,
            policy=build_business_policy(self.executor.registry),
            goal_validator=ProductionGoalAdapter(action_planner),
            backoff_jitter=0,
        )
        state = prior_state or RuntimeState(
            run_id=task.task_id,
            thread_id=task.thread_id,
            user_id=task.user_id,
            tenant_id=task.tenant_id,
            goal=str(task.goal.value or ""),
            acceptance_criteria=list(task.acceptance_criteria.value or ["validated result"]),
            task_envelope=task,
            slot_states=task.slot_states(),
            budget=budget or RunBudget(max_steps=30, max_tool_calls=30),
        )
        result = await runtime.run(state)
        final = result.final_state
        observations = final.normalized_observations
        evidence = [
            value
            for observation in observations.values()
            for value in observation.get("evidence", [])
        ]
        draft = observations.get("draft", {}).get("data", {})
        revision = observations.get("revise", {}).get("data", {})
        return final, FinalJourneyResult(
            run_id=final.run_id,
            status=final.status.value,
            reason_code=final.reason_code,
            article=observations.get("article", {}).get("data", {}).get("article", {}),
            classification=observations.get("classify", {}).get("data", {}),
            products=observations.get("products", {}).get("data", {}),
            score=observations.get("score", {}).get("data", {}),
            artifact=draft.get("artifact") or revision.get("artifact") or {},
            review=observations.get("review", {}).get("data", {}) or revision.get("review", {}),
            assumptions=[item.text for item in task.assumptions],
            evidence=evidence,
            pending_questions=final.pending_questions,
            completed_steps=final.completed_steps,
        )
