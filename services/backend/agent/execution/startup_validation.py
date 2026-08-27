"""StartupValidator - 启动期矩阵硬校验（OneShot Cutover 计划 §34 / §35 / §36）。

在 FastAPI startup 与 Worker startup 阶段对装配结果执行 §35 矩阵校验，
任何失败立即 fail-fast，禁止等待任务进来才报错（§36）。

矩阵（§35）：

    | 模式           | 硬性要求                                                  |
    |----------------|-----------------------------------------------------------|
    | legacy         | legacy_executor != None（执行侧）；skill 可 None           |
    | skill_shadow   | legacy + skill_executor + shadow_executor != None          |
    | skill_canary   | legacy + skill_executor + rollout != None                  |
    | skill_planned  | legacy == None；skill_executor/skill_runtime/orchestration_runtime/business_executor/artifact_store != None |

FastAPI 侧（executes_tasks=False）不执行任务，因此不强制 legacy_executor；
Worker 侧（executes_tasks=True）执行任务，必须完整满足矩阵。
"""

from __future__ import annotations

from typing import Any

from agent.execution.router import ExecutionRouter

_VALID_MODES = frozenset({"legacy", "skill_shadow", "skill_canary", "skill_planned"})


class StartupValidationError(RuntimeError):
    """启动矩阵校验失败。"""


def _need_legacy(mode: str) -> bool:
    return mode in {"legacy", "skill_shadow", "skill_canary"}


def _need_skill(mode: str) -> bool:
    return mode in {"skill_shadow", "skill_canary", "skill_planned"}


def validate_runtime(
    runtime: Any,
    mode: str,
    *,
    executes_tasks: bool = False,
) -> bool:
    """校验 runtime 是否满足 §35 矩阵。失败抛 StartupValidationError（fail-fast）。

    Args:
        runtime: ProductionExecutionRuntime（或字段兼容对象）。
        mode: AGENT_EXECUTION_MODE。
        executes_tasks: 该进程是否执行任务（Worker=True；FastAPI main=False）。
    """
    if mode not in _VALID_MODES:
        raise StartupValidationError(f"unknown AGENT_EXECUTION_MODE: {mode}")
    if not isinstance(runtime.execution_router, ExecutionRouter):
        raise StartupValidationError("runtime.execution_router 必须是 ExecutionRouter")

    need_legacy = _need_legacy(mode)
    need_skill = _need_skill(mode)
    issues: list[str] = []

    # ── Skill 侧（need_skill 时全部必须就位，§35 / §91）──
    if need_skill:
        if runtime.skill_executor is None:
            issues.append("skill_loaded=False 但模式需要 skill_executor")
        if runtime.skill_runtime is None:
            issues.append("mode 需要 skill_runtime")
        if runtime.orchestration_runtime is None:
            issues.append("mode 需要 orchestration_runtime")
        if runtime.business_executor is None:
            issues.append("mode 需要 business_executor")
        if runtime.artifact_store is None:
            issues.append("mode 需要 artifact_store")
        if runtime.skill_loaded is not True:
            issues.append("mode 需要 skill_loaded=True")

    # ── Shadow / Canary 专属（§8 / §35）──
    if mode == "skill_shadow" and runtime.shadow_executor is None:
        issues.append("skill_shadow 需要 shadow_executor")
    if mode == "skill_canary" and runtime.rollout is None:
        issues.append("skill_canary 需要 rollout")

    # ── Legacy 侧（§35 / §94）──
    if executes_tasks:
        if need_legacy and runtime.legacy_executor is None:
            issues.append("执行侧 legacy 模式需要 legacy_executor")
        if mode == "skill_planned" and runtime.legacy_executor is not None:
            issues.append("skill_planned 不得加载 legacy_executor")
    if mode == "skill_planned" and runtime.legacy_loaded is not False:
        issues.append("skill_planned 要求 legacy_loaded=False")

    if issues:
        detail = " | ".join(issues)
        raise StartupValidationError(f"Startup matrix FAILED (mode={mode}): {detail}")
    return True


__all__ = ["StartupValidationError", "validate_runtime"]
