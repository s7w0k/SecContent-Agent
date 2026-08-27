"""SkillPlannedExecutor - 以 OrchestrationRuntime 为新主执行引擎（§2.2 / §13）。

新主链：ExecutionRequest → OrchestrationRuntime.run → OrchestratorState →
ExecutionResultAdapter → ExecutionResult。Resume 采用幂等 replay（§66）。"""

from __future__ import annotations

import logging
import time

from agent.execution.contracts import (
    ExecutionRequest,
    ExecutionResult,
)
from agent.execution.errors import ResumeNotSupported
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
    ) -> None:
        self.runtime = orchestration_runtime
        self.result_adapter = result_adapter or ExecutionResultAdapter()
        self.shadow = shadow

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        start = time.monotonic()
        state = await self.runtime.run(
            goal=request.goal,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            trace_id=request.trace_id,
        )
        result = await self.result_adapter.from_orchestrator(request=request, state=state)
        result.latency_ms = (time.monotonic() - start) * 1000
        result.metadata["shadow"] = self.shadow
        return result

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        # §66：第一阶段只支持基于已存 Artifact/ledger 的幂等 replay，不复刻 checkpoint。
        # 对已完成的 run，重复执行是幂等的；未落 checkpoint 时显式返回不支持。
        raise ResumeNotSupported("skill_planned")


__all__ = ["SkillPlannedExecutor"]
