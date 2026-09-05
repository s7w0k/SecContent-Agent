"""Deterministic per-step and final workflow validation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from agent.observation import NormalizedObservation, ObservationStatus
from agent.production_plan import Plan, PlanStep
from pydantic import BaseModel, Field


class WorkflowDecision(StrEnum):
    COMPLETE = "complete"
    CONTINUE = "continue"
    REPLAN = "replan"
    ASK_USER = "ask_user"
    WAIT_APPROVAL = "wait_approval"
    STOP = "stop"


class ValidationDecision(BaseModel):
    decision: WorkflowDecision
    reason_code: str
    reason: str
    evidence: list[str] = Field(default_factory=list)


def _lookup(data: dict[str, Any], dotted: str) -> Any:
    value: Any = data
    for part in dotted.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


class StepValidator:
    def validate(self, step: PlanStep, observation: NormalizedObservation) -> ValidationDecision:
        if observation.status == ObservationStatus.FAILED:
            return ValidationDecision(
                decision=WorkflowDecision.REPLAN
                if observation.retryable
                else WorkflowDecision.STOP,
                reason_code=observation.reason_code or "tool_failed",
                reason="tool observation failed",
            )
        # Empty collections are valid observations for search/list tools and drive
        # explicit no-result branches. Missing/null fields are schema failures.
        missing = [
            path
            for path in step.expected_observation
            if _lookup(observation.data, path) in (None, "")
        ]
        if missing:
            return ValidationDecision(
                decision=WorkflowDecision.REPLAN,
                reason_code="expected_observation_missing",
                reason=f"missing observation fields: {missing}",
                evidence=observation.evidence,
            )
        if observation.status == ObservationStatus.PARTIAL:
            return ValidationDecision(
                decision=WorkflowDecision.REPLAN,
                reason_code="partial_success",
                reason="step partially succeeded and requires a recovery plan",
                evidence=observation.evidence,
            )
        return ValidationDecision(
            decision=WorkflowDecision.CONTINUE,
            reason_code="step_accepted",
            reason="step postconditions satisfied",
            evidence=observation.evidence,
        )


class WorkflowGoalValidator:
    """Validate artifacts and review policy; never trusts a model completion claim."""

    def validate(
        self,
        plan: Plan,
        observations: dict[str, NormalizedObservation],
        *,
        approval_pending: bool = False,
        missing_required_slots: list[str] | None = None,
        save_requires_approval: bool = False,
    ) -> ValidationDecision:
        if missing_required_slots:
            return ValidationDecision(
                decision=WorkflowDecision.ASK_USER,
                reason_code="required_slots_missing",
                reason=f"required slots missing: {sorted(missing_required_slots)}",
            )
        if approval_pending or save_requires_approval:
            return ValidationDecision(
                decision=WorkflowDecision.WAIT_APPROVAL,
                reason_code="waiting_approval",
                reason="an approved save or high-risk action is pending",
            )
        required = [step for step in plan.steps if step.status.value != "skipped"]
        missing_steps = [step.step_id for step in required if step.step_id not in observations]
        if missing_steps:
            return ValidationDecision(
                decision=WorkflowDecision.CONTINUE,
                reason_code="steps_remaining",
                reason=f"steps have no accepted observation: {missing_steps}",
            )
        failed = [step_id for step_id, obs in observations.items() if not obs.ok]
        if failed:
            return ValidationDecision(
                decision=WorkflowDecision.REPLAN,
                reason_code="failed_steps",
                reason=f"failed steps require replan: {failed}",
            )

        tools = {step.tool: step.step_id for step in plan.steps}
        draft_step = tools.get("generate_draft") or tools.get("revise_draft")
        if draft_step:
            draft = observations[draft_step].data.get("artifact") or {}
            if not draft or not draft.get("artifact_id") or not draft.get("content_hash"):
                return ValidationDecision(
                    decision=WorkflowDecision.REPLAN,
                    reason_code="draft_artifact_missing",
                    reason="draft artifact is absent or empty",
                )
            review_step = tools.get("review_draft")
            if not review_step:
                return ValidationDecision(
                    decision=WorkflowDecision.REPLAN,
                    reason_code="review_step_missing",
                    reason="draft workflow must include review",
                )
            review = observations[review_step].data
            if not review.get("passed", False):
                return ValidationDecision(
                    decision=WorkflowDecision.REPLAN,
                    reason_code="draft_review_failed",
                    reason="draft has unresolved review issues",
                )
        evidence = [item for obs in observations.values() for item in obs.evidence]
        return ValidationDecision(
            decision=WorkflowDecision.COMPLETE,
            reason_code="goal_validated",
            reason="all acceptance checks have machine-verifiable observations",
            evidence=evidence,
        )
