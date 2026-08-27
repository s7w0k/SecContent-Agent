"""Orchestration Layer 契约 - SkillPlan / OrchestratorChoice / OrchestratorState。

计划 §29 / §30 / §32 / §54。规划单位从 Worker Step 升级为 Skill Step（§27）。
LLM 只输出受约束的 OrchestratorChoice，服务端据 INTENT_SKILLS 构建 SkillPlan。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Intent = Literal[
    "curate_news",
    "score_article",
    "generate_draft",
    "review_draft",
    "revise_draft",
    "full_workflow",
]

OrchestratorStatus = Literal[
    "PLANNING",
    "RUNNING",
    "WAITING_REVIEW",
    "WAITING_APPROVAL",
    "COMPLETED",
    "FAILED",
    "BLOCKED",
]


class SkillPlanStep(BaseModel):
    """计划 §29：SkillPlan 中的一步。"""

    step_id: str
    skill_name: str = Field(..., pattern=r"^[a-z0-9-]+$")
    depends_on: list[str] = Field(default_factory=list)
    input_refs: dict[str, str] = Field(default_factory=dict)
    condition: str = ""
    max_attempts: int = Field(default=1, ge=1, le=5)


class SkillPlan(BaseModel):
    """计划 §29：服务端据 Skill Contract 构建的 Skill 级执行计划。"""

    plan_id: str
    run_id: str
    goal: str
    intent: str = ""
    steps: list[SkillPlanStep] = Field(default_factory=list)
    planner_version: str = "1.0.0"
    skill_snapshot_hash: str = ""
    tool_registry_hash: str = ""

    def step(self, step_id: str) -> SkillPlanStep | None:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None


class OrchestratorChoice(BaseModel):
    """计划 §30：LLM 唯一允许输出的规划选择（服务端白名单决定 Skill）。"""

    intent: Intent = Field(..., description="服务端 INTENT_SKILLS 白名单内的意图")
    article_refs: list[str] = Field(default_factory=list)
    product_ids: list[str] = Field(default_factory=list)
    desired_output: str = ""
    rationale_summary: str = ""


class OrchestratorState(BaseModel):
    """计划 §32：Orchestrator 运行态（持久化在 run_id 下）。"""

    run_id: str
    goal: str
    plan_ref: str = ""
    completed_steps: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    reviewer_rounds: int = Field(default=0, ge=0)
    replan_count: int = Field(default=0, ge=0)
    status: OrchestratorStatus = "PLANNING"


class OrchestratorBudget(BaseModel):
    """计划 §54：Orchestrator 整体预算（推荐值）。"""

    max_agent_rounds: int = Field(default=8, ge=1)
    max_replans: int = Field(default=2, ge=0)
    max_skill_calls: int = Field(default=12, ge=1)
    max_review_rounds: int = Field(default=2, ge=0)
    max_total_tool_calls: int = Field(default=40, ge=1)
    max_runtime_seconds: int = Field(default=600, ge=1)


__all__ = [
    "Intent",
    "OrchestratorBudget",
    "OrchestratorChoice",
    "OrchestratorState",
    "OrchestratorStatus",
    "SkillPlan",
    "SkillPlanStep",
]
