"""SkillPlannedExecutor - 以 OrchestrationRuntime 为新主执行引擎（§2.2 / §13）。

新主链：ExecutionRequest → OrchestrationRuntime.run → OrchestratorState →
ExecutionResultAdapter → ExecutionResult。Resume 采用幂等 replay（§66）。"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent.execution.contracts import (
    ExecutionRequest,
    ExecutionResult,
)
from agent.execution.result_adapter import ExecutionResultAdapter
from agent.orchestration.runtime import OrchestrationRuntime

logger = logging.getLogger("backend.agent.execution.skill")


class SkillPlannedExecutor:
    """把 OrchestrationRuntime 包进统一 WorkflowExecutor 契约。"""

    def __init__(
        self,
        *,
        orchestration_runtime: OrchestrationRuntime,
        result_adapter: ExecutionResultAdapter | None = None,
        shadow: bool = False,
        run_store: Any | None = None,
    ) -> None:
        self.runtime = orchestration_runtime
        self.result_adapter = result_adapter or ExecutionResultAdapter()
        self.shadow = shadow
        self.run_store = run_store or orchestration_runtime.run_store

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        start = time.monotonic()
        state = await self.runtime.run(
            goal=request.goal,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            trace_id=request.trace_id,
            task_id=request.task_id,
        )
        result = await self.result_adapter.from_orchestrator(request=request, state=state)
        result.latency_ms = (time.monotonic() - start) * 1000
        result.metadata["shadow"] = self.shadow
        return result

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        # Final Closure（EPIC-A §14）：幂等恢复，不再 raise ResumeNotSupported。
        if self.run_store is None:
            from agent.execution.errors import ResumeNotSupported

            raise ResumeNotSupported("skill_planned:no_run_store")
        from agent.execution.run_store import ResumeStateNotFound

        record = await self.run_store.get_by_task(request.task_id)
        if record is None:
            raise ResumeStateNotFound(request.task_id)
        start = time.monotonic()
        state = await self.runtime.resume(
            run_record=record,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            trace_id=request.trace_id,
        )
        result = await self.result_adapter.from_orchestrator(request=request, state=state)
        result.latency_ms = (time.monotonic() - start) * 1000
        result.metadata["shadow"] = self.shadow
        result.metadata["resumed"] = True
        return result


__all__ = ["SkillPlannedExecutor"]
