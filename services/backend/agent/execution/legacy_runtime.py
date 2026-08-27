"""LegacyRuntimeBundle - 旧链路显式回滚通道（OneShot Cutover 计划 §9 / §22 / §83）。

本次不重写/不删除旧 PipelineManagerV2 与 old MultiAgentRuntime（§83），只把它们
包进可回滚的 Legacy Runtime（§10 封装后退役）。仅在显式 legacy 模式下作为回滚通道（Final Closure §46-47）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.execution.legacy_executor import LegacyPipelineExecutor


@dataclass
class LegacyRuntimeBundle:
    """旧链执行上下文（§9）。"""

    pipeline_v2: Any = None
    pipeline_manager: Any = None
    old_multi_agent: Any = None
    executor: LegacyPipelineExecutor | None = None
    classifier_v2: Any = None
    scorer_v2: Any = None
    draft_generator: Any = None
    draft_reviewer: Any = None
    template_repository: Any = None
    notes: list[str] = field(default_factory=list)


def build_legacy_runtime(
    *,
    pipeline_v2: Any = None,
    pipeline_manager: Any = None,
    old_multi_agent: Any = None,
    executor: LegacyPipelineExecutor | None = None,
    classifier_v2: Any = None,
    scorer_v2: Any = None,
    draft_generator: Any = None,
    draft_reviewer: Any = None,
    template_repository: Any = None,
) -> LegacyRuntimeBundle:
    """按需装配 Legacy 运行时；skill_planned 模式下不调用本函数（§23 / §94）。"""
    return LegacyRuntimeBundle(
        pipeline_v2=pipeline_v2,
        pipeline_manager=pipeline_manager,
        old_multi_agent=old_multi_agent,
        executor=executor,
        classifier_v2=classifier_v2,
        scorer_v2=scorer_v2,
        draft_generator=draft_generator,
        draft_reviewer=draft_reviewer,
        template_repository=template_repository,
    )


__all__ = ["LegacyRuntimeBundle", "build_legacy_runtime"]
