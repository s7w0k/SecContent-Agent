"""ExecutionResultAdapter - Orchestrator Artifact → 统一 ExecutionResult（§14 / §15）。

新 Runtime 生成 ArtifactRef；Adapter 负责把它们映射回统一结果/兼容既有业务模型，
Skill 自身不感知 legacy 数据模型。
"""

from __future__ import annotations

from typing import Any

from agent.execution.contracts import ExecutionRequest, ExecutionResult, ExecutionStatus
from agent.orchestration.contracts import OrchestratorState

_StatusMap: dict[str, ExecutionStatus] = {
    "COMPLETED": "SUCCEEDED",
    "PLANNING": "PARTIAL",
    "RUNNING": "PARTIAL",
    "WAITING_REVIEW": "PARTIAL",
    "WAITING_APPROVAL": "PARTIAL",
    "FAILED": "FAILED",
}


def _map_status(status: str) -> ExecutionStatus:
    return _StatusMap.get(status, "PARTIAL")


class ExecutionResultAdapter:
    """把 OrchestratorState（含 ArtifactRef）转换为统一 ExecutionResult。"""

    async def from_orchestrator(
        self,
        *,
        request: ExecutionRequest,
        state: OrchestratorState,
    ) -> ExecutionResult:
        artifact_refs = sorted(str(v) for v in state.artifact_refs.values())
        output: dict[str, Any] = {
            "goal": state.goal,
            "plan_ref": state.plan_ref,
            "completed_steps": list(state.completed_steps),
            "failed_steps": list(state.failed_steps),
            "reviewer_rounds": state.reviewer_rounds,
            "replan_count": state.replan_count,
            "status": state.status,
            "artifact_refs": artifact_refs,
        }
        return ExecutionResult(
            run_id=state.run_id,
            task_id=request.task_id,
            status=_map_status(state.status),
            engine="skill_planned",
            artifact_refs=artifact_refs,
            output=output,
            error_code=None if state.status != "FAILED" else "orchestrator_failed",
            error_message=None,
            trace_id=request.trace_id,
            metadata=dict(request.metadata),
        )


__all__ = ["ExecutionResultAdapter"]
