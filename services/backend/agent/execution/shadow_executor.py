"""ShadowExecutor - Legacy 权威 + Skill 只读双跑（§34-38 / §2.3）。

Shadow Skill Path 不允许任何生产副作用：只读业务工具（production_readonly adapter）
产出临时 Artifact，比较 + 评估，Legacy primary 权威返回。Skill shadow 异常不影响 primary。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent.execution.comparator import ShadowComparator, ShadowEvaluation
from agent.execution.contracts import ExecutionRequest, ExecutionResult, WorkflowExecutor
from agent.execution.metrics import ExecutionMetricsClient
from agent.execution.rollout import ShadowSampler

logger = logging.getLogger("backend.agent.execution.shadow")


class ShadowExecutor:
    """在 legacy 权威执行的同时并行双跑只读 skill 路径。"""

    def __init__(
        self,
        *,
        legacy: WorkflowExecutor,
        skill: WorkflowExecutor,
        comparator: ShadowComparator | None = None,
        sampler: ShadowSampler | None = None,
        metrics: ExecutionMetricsClient | None = None,
        shadow_timeout_seconds: float = 60.0,
        store: Any | None = None,
    ) -> None:
        self.legacy = legacy
        self.skill = skill
        self.comparator = comparator or ShadowComparator()
        self.sampler = sampler or ShadowSampler(sample_percent=100)
        self.metrics = metrics or ExecutionMetricsClient()
        self.shadow_timeout_seconds = shadow_timeout_seconds
        self.store = store
        self._write_guarded = True

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        legacy_task = asyncio.create_task(self.legacy.execute(request))
        shadow_task = None
        if self.sampler.should_sample(request):
            # skill 侧以只读语义执行 —— 写工具被 production_readonly 拒绝（§36）
            shadow_task = asyncio.create_task(self._safe_shadow(request))

        primary = await legacy_task

        shadow = None
        if shadow_task is not None:
            shadow = await shadow_task

        try:
            configuration = await self.comparator.compare(
                request=request,
                primary=primary,
                shadow=shadow,
            )
        except Exception as exc:  # 比较失败不影响 primary（§47）
            logger.warning("shadow compare failed: %s", exc)
            configuration = ShadowEvaluation(
                task_id=request.task_id,
                trace_id=request.trace_id,
                legacy_status=primary.status,
                skill_status=str(getattr(shadow, "status", "ERROR")),
            )
        await self._emit_evaluation(request, configuration)
        return primary

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        # Shadow 只做评估，resume 语义交给 legacy primary
        return await self.legacy.resume(request)

    async def _safe_shadow(self, request: ExecutionRequest) -> ExecutionResult | None:
        """执行只读 skill 路径，超时/异常均吞掉并返回 None，绝不影响 primary。"""
        try:
            return await asyncio.wait_for(
                self.skill.execute(request),
                timeout=self.shadow_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("shadow skill timed out: task_id=%s", request.task_id)
            self.metrics.record(metric="shadow_aborted", engine="skill_planned")
            return None
        except Exception as exc:  # 只读路径失败不得回退 legacy 或影响 primary
            logger.warning("shadow skill failed: %s", exc)
            self.metrics.record(metric="shadow_failed", engine="skill_planned")
            return None

    async def _emit_evaluation(
        self,
        request: ExecutionRequest,
        evaluation: ShadowEvaluation,
    ) -> None:
        self.metrics.record(metric="shadow_total", engine="legacy")
        if evaluation.critical_mismatch:
            self.metrics.record(metric="shadow_critical_mismatch", engine="skill_planned")
        if self.store is not None:
            try:
                await self.store(evaluation)
            except Exception as exc:  # 存储失败不影响 primary
                logger.warning("shadow evaluation persist failed: %s", exc)
        logger.info(
            "shadow evaluation: task_id=%s critical=%s",
            request.task_id,
            evaluation.critical_mismatch,
        )


__all__ = ["ShadowExecutor"]
