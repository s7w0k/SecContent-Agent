"""LegacyPipelineExecutor - 包装旧 Worker DAG 执行链。

职责（§9 / §10）：把 Legacy 执行（PipelineManagerV2 / old MultiAgentRuntime /
api.pipeline helper）包进统一 WorkflowExecutor 契约，使 ExecutionRouter 不必感知
旧组件的存在。Legacy 只允许显式选择（§85），本文件是 plan 允许的 legacy 位置。

默认通过注入 runner 隔离旧链：runner 由装配侧（worker factory）绑定 worker ctx，
因此本文件不 import api.pipeline（§10 / §126 硬门禁适用的是 task_queue 等主路径）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from agent.execution.contracts import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
)

logger = logging.getLogger("backend.agent.execution.legacy")

_StatusMap: dict[str, ExecutionStatus] = {
    "completed": "SUCCEEDED",
    "succeeded": "SUCCEEDED",
    "success": "SUCCEEDED",
    "partial": "PARTIAL",
    "blocked": "BLOCKED",
    "cancelled": "FAILED",
    "rejected": "BLOCKED",
    "failed": "FAILED",
}


def _map_status(raw: str | None) -> ExecutionStatus:
    if raw in _StatusMap:
        return _StatusMap[raw]
    if raw in ("failed", "cancelled"):
        logger.warning("legacy runner returned failure status=%s", raw)
        return "PARTIAL"
    return "PARTIAL"


class LegacyPipelineExecutor:
    """统一契约下的 Legacy 执行器。

    Args:
        execute_runner: 由装配侧注入的旧执行函数，签名：
            ``async def runner(request: ExecutionRequest) -> dict``。
            返回的 dict 至少含 ``{"status": str}``，可含 ``output/artifact_refs/error``。
            异常会向上抛出（保持 ARQ Retry 语义）。
        resume_runner: 可选。旧 checkpoint resume 函数，签名同 execute_runner。
            为 None 时 resume() 抛 ``LegacyNotAvailable``。
    """

    def __init__(
        self,
        *,
        execute_runner: Callable[[ExecutionRequest], Awaitable[dict[str, Any]]],
        resume_runner: Callable[[ExecutionRequest], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._execute_runner = execute_runner
        self._resume_runner = resume_runner

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        start = time.monotonic()
        raw = await self._execute_runner(request)
        return self._to_result(request, raw, start)

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        if self._resume_runner is None:
            raise RuntimeError("legacy resume runner is not configured")
        start = time.monotonic()
        raw = await self._resume_runner(request)
        return self._to_result(request, raw, start)

    def _to_result(
        self,
        request: ExecutionRequest,
        raw: dict[str, Any],
        start: float,
    ) -> ExecutionResult:
        artifact_refs = raw.get("artifact_refs")
        if not isinstance(artifact_refs, list):
            artifact_refs = []
        output = raw.get("output")
        if not isinstance(output, dict):
            output = {k: v for k, v in raw.items() if k not in ("artifact_refs", "status")}
        return ExecutionResult(
            run_id=request.run_id or raw.get("run_id", request.task_id),
            task_id=request.task_id,
            status=_map_status(raw.get("status")),
            engine="legacy",
            artifact_refs=[str(a) for a in artifact_refs],
            output=output,
            error_code=raw.get("error_code"),
            error_message=str(raw.get("error_message") or raw.get("error") or "") or None,
            trace_id=request.trace_id,
            latency_ms=(time.monotonic() - start) * 1000,
            metadata={"legacy_plan_source": raw.get("plan_source"), **request.metadata},
        )


__all__ = ["LegacyPipelineExecutor"]
