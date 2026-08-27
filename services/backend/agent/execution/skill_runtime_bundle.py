"""SkillRuntimeBundle - 新 Skill Runtime 完整装配（OneShot Cutover 计划 §12 / §13 / §91）。

完整生产链路（§13）：

    ProductionBusinessToolService
        ↓
    BusinessToolRegistry
        ↓
    BusinessToolExecutor
        ↓
    ExecutableSkillRegistry
        ↓
    MongoArtifactStore
        ↓
    SkillRuntime
        ↓
    OrchestrationRuntime
        ↓
    SkillPlannedExecutor

本模块把这些组件组合成一个可注入的 Skill Runtime（§12），供生产唯一装配入口
（build_production_execution_runtime）使用。Skill 不可用时本 bundle 不构造成形，
由生产装配按 AGENT_EXECUTION_MODE 决定是否调用（§8 矩阵 / §22）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.business_tools.contracts import BusinessToolRegistry
from agent.business_tools.execution import BusinessToolExecutor
from agent.execution.factory import (
    build_executable_skill_registry,
    build_orchestration_runtime,
    build_skill_runtime,
)
from agent.execution.result_adapter import ExecutionResultAdapter
from agent.execution.skill_executor import SkillPlannedExecutor
from agent.orchestration.runtime import OrchestrationRuntime
from agent.skills.executable_registry import ExecutableSkillRegistry
from agent.skills.runtime import SkillRuntime


@dataclass
class SkillRuntimeBundle:
    """新 Skill 执行上下文（§12）。"""

    business_registry: BusinessToolRegistry
    business_executor: BusinessToolExecutor

    skill_registry: ExecutableSkillRegistry
    skill_runtime: SkillRuntime

    orchestration_runtime: OrchestrationRuntime
    skill_executor: SkillPlannedExecutor

    artifact_store: Any

    @staticmethod
    def skill_count() -> int:
        return 5


def build_skill_runtime_bundle(
    *,
    business_registry: BusinessToolRegistry,
    business_executor: BusinessToolExecutor,
    artifact_store: Any,
    default_adapter: str = "production",
    trace_emitter: Any | None = None,
) -> SkillRuntimeBundle:
    """完整装配新 Skill Runtime（§13）。

    Args:
        business_registry: 生产业务 Tool Registry（§14 Worker 与 main 一致）。
        business_executor: 业务 Tool 执行器。
        artifact_store: 产物存储（生产 MongoArtifactStore / 测试内存 ArtifactStore）。
        default_adapter: 默认 Tool adapter。shadow 模式用 production_readonly（§26）。
        trace_emitter: 可选 span 发射器。

    Returns:
        完整可执行的 SkillRuntimeBundle。
    """
    registry = build_executable_skill_registry(business_registry=business_registry)
    scopes: set[str] = set()
    for name in registry.names():
        scopes.update(registry.get_manifest(name).required_scopes)
    scopeset = frozenset(scopes)

    skill_runtime = build_skill_runtime(
        registry=registry,
        business_executor=business_executor,
        artifact_store=artifact_store,
        default_adapter=default_adapter,
        trace_emitter=trace_emitter,
    )
    orchestration_runtime = build_orchestration_runtime(
        skill_runtime=skill_runtime,
        scopes=scopeset,
        trace_emitter=trace_emitter,
    )
    skill_executor = SkillPlannedExecutor(
        orchestration_runtime=orchestration_runtime,
        result_adapter=ExecutionResultAdapter(),
        shadow=default_adapter == "production_readonly",
    )
    return SkillRuntimeBundle(
        business_registry=business_registry,
        business_executor=business_executor,
        skill_registry=registry,
        skill_runtime=skill_runtime,
        orchestration_runtime=orchestration_runtime,
        skill_executor=skill_executor,
        artifact_store=artifact_store,
    )


__all__ = ["SkillRuntimeBundle", "build_skill_runtime_bundle"]
