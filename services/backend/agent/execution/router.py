"""ExecutionRouter - 生产执行入口（§29 / §30-32 / §34）。

四种模式（§28）：legacy / skill_shadow / skill_canary / skill_planned。

不变量（§30 / §104）：Skill 失败绝不隐式回退 Legacy；Router 只做 request 级预选，
Retry/Resume 复用已选 engine，保持 sticky（§31-32）。
"""

from __future__ import annotations

import logging
from typing import Any

from agent.execution.contracts import (
    EXECUTION_ENGINES,
    ExecutionEngine,
    ExecutionRequest,
    ExecutionResult,
    WorkflowExecutor,
)
from agent.execution.errors import EngineNotConfigured, UnsupportedExecutionMode
from agent.execution.metrics import ExecutionMetricsClient

logger = logging.getLogger("backend.agent.execution.router")


class ExecutionRouter:
    """按 AGENT_EXECUTION_MODE 分发到对应 WorkflowExecutor。"""

    def __init__(
        self,
        *,
        mode: str,
        legacy: WorkflowExecutor | None,
        skill: WorkflowExecutor | None,
        shadow: WorkflowExecutor | None = None,
        rollout: Any | None = None,
        metrics: ExecutionMetricsClient | None = None,
    ) -> None:
        if mode not in EXECUTION_ENGINES and mode not in ("skill_shadow", "skill_canary"):
            raise UnsupportedExecutionMode(mode)
        self.mode = mode
        self.legacy = legacy
        self.skill = skill
        self.shadow = shadow
        self.rollout = rollout
        self.metrics = metrics or ExecutionMetricsClient()

    # ── 主入口 ──────────────────────────────────────────

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        engine, executor, tied = self._select(request)
        if engine == "skill_shadow" or tied == "shadow":
            assert self.shadow is not None  # 由构造保证
            result = await self.shadow.execute(request)
        else:
            result = await executor.execute(request)
        result.engine = engine if engine in EXECUTION_ENGINES else "legacy"
        result.metadata["execution_mode"] = self.mode
        result.metadata["selected_engine"] = engine
        self.metrics.record(metric="execution_total", engine=result.engine)
        self.metrics.record(
            metric="execution_" + result.status.lower(),
            engine=result.engine,
            latency_ms=result.latency_ms,
        )
        return result

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        # §32：读取任务创建时选定的 engine，不重新 rollout
        engine = request.selected_engine or self._resolve_for_resume()
        if engine == "skill_shadow":
            # shadow 是评估层；resume 语义交给 legacy primary
            assert self.shadow is not None
            return await self.shadow.resume(request)
        executor = self._executor_for(engine)
        return await executor.resume(request)

    def select_engine(self, request: ExecutionRequest) -> ExecutionEngine:
        """任务创建时调用，把选定 engine 写入 task state（§31-32）。"""
        engine, _, _ = self._select(request)
        return engine if engine in EXECUTION_ENGINES else "legacy"

    # ── 内部 ────────────────────────────────────────────

    def _select(self, request: ExecutionRequest) -> tuple[str, WorkflowExecutor | None, str]:
        mode = self.mode
        if request.selected_engine is not None:
            # sticky：任务已选定过 engine，复用（§63）
            engine = request.selected_engine
            return engine, self._executor_for(engine), "sticky"
        if mode == "legacy":
            return "legacy", self.legacy, "mode"
        if mode == "skill_shadow":
            return "skill_shadow", None, "shadow"
        if mode == "skill_canary":
            engine = self.rollout.choose(request) if self.rollout is not None else "legacy"
            return engine, self._executor_for(engine), "canary"
        if mode == "skill_planned":
            return "skill_planned", self._executor_for("skill_planned"), "mode"
        raise UnsupportedExecutionMode(mode)

    def _executor_for(self, engine: str) -> WorkflowExecutor:
        executor = {"legacy": self.legacy, "skill_planned": self.skill}.get(engine)
        if executor is None:
            raise EngineNotConfigured(engine)
        return executor

    def _resolve_for_resume(self) -> str:
        # 未带 selected_engine 时按模式取默认 engine，不含 rollout 随机性
        if self.mode == "skill_planned":
            return "skill_planned"
        if self.mode == "skill_canary":
            return "skill_planned" if self.rollout and self.rollout.percent >= 100 else "legacy"
        if self.mode == "skill_shadow":
            return "legacy"
        return "legacy"


__all__ = ["ExecutionRouter"]
