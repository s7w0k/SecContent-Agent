"""Execution Layer - 统一生产执行入口（Cutover 计划 §4）。

四层：ExecutionRouter → WorkflowExecutor(Legacy/Skill/Shadow) → SkillPlannedExecutor →
OrchestrationRuntime → SkillRuntime → BusinessToolExecutor。默认模式按配置保持 legacy。
"""

from __future__ import annotations

from agent.execution.comparator import ShadowComparator, ShadowEvaluation
from agent.execution.contracts import (
    EXECUTION_ENGINES,
    ExecutionEngine,
    ExecutionRequest,
    ExecutionResult,
    ExecutionRuntimeBundle,
    ExecutionStatus,
    WorkflowExecutor,
)
from agent.execution.errors import (
    EngineNotConfigured,
    ExecutionError,
    LegacyNotAvailable,
    UnsupportedExecutionMode,
)
from agent.execution.factory import (
    build_executable_skill_registry,
    build_execution_runtime,
    build_orchestration_runtime,
    build_skill_runtime,
)
from agent.execution.legacy_executor import LegacyPipelineExecutor
from agent.execution.metrics import ExecutionMetrics, ExecutionMetricsClient
from agent.execution.result_adapter import ExecutionResultAdapter
from agent.execution.rollout import LevelCanaryRollout, ShadowSampler, stable_hash
from agent.execution.router import ExecutionRouter
from agent.execution.shadow_executor import ShadowExecutor
from agent.execution.skill_executor import SkillPlannedExecutor

__all__ = [
    "EXECUTION_ENGINES",
    "EngineNotConfigured",
    "ExecutionEngine",
    "ExecutionError",
    "ExecutionMetrics",
    "ExecutionMetricsClient",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionResultAdapter",
    "ExecutionRouter",
    "ExecutionRuntimeBundle",
    "ExecutionStatus",
    "LegacyNotAvailable",
    "LegacyPipelineExecutor",
    "LevelCanaryRollout",
    "ShadowComparator",
    "ShadowEvaluation",
    "ShadowExecutor",
    "ShadowSampler",
    "SkillPlannedExecutor",
    "UnsupportedExecutionMode",
    "WorkflowExecutor",
    "build_executable_skill_registry",
    "build_execution_runtime",
    "build_orchestration_runtime",
    "build_skill_runtime",
    "stable_hash",
]
