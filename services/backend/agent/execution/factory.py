"""build_execution_runtime - FastAPI / Worker 共享的执行运行时装配（§16-18 / §22-26）。

FastAPI 与 Worker 必须使用同一个 Factory（§24），且技能装配在启动时任一失败必须
fail-fast，不允许 log warning 后继续（§16）。默认模式保持 legacy（§27 / 所选实施范围）。
"""

from __future__ import annotations

import logging
from typing import Any

from agent.business_tools.contracts import BusinessToolRegistry
from agent.business_tools.execution import BusinessToolExecutor
from agent.execution.comparator import ShadowComparator
from agent.execution.contracts import (
    ExecutionRuntimeBundle,
    WorkflowExecutor,
)
from agent.execution.metrics import ExecutionMetricsClient
from agent.execution.result_adapter import ExecutionResultAdapter
from agent.execution.rollout import LevelCanaryRollout, ShadowSampler
from agent.execution.router import ExecutionRouter
from agent.execution.shadow_executor import ShadowExecutor
from agent.execution.skill_executor import SkillPlannedExecutor
from agent.orchestration.runtime import OrchestrationRuntime
from agent.skills.executable_registry import ExecutableSkillRegistry
from agent.skills.runtime import SkillRuntime

logger = logging.getLogger("backend.agent.execution.factory")


def _registry_scopes(registry: ExecutableSkillRegistry) -> frozenset[str]:
    scopes: set[str] = set()
    for name in registry.names():
        manifest = registry.get_manifest(name)
        scopes.update(manifest.required_scopes)
    return frozenset(scopes)


def build_executable_skill_registry(
    *,
    business_registry: BusinessToolRegistry,
) -> ExecutableSkillRegistry:
    """注册生产五技能（§16）。任一注册失败即抛错（startup FAILED）。"""
    registry = ExecutableSkillRegistry(business_tool_names=tuple(business_registry.names()))
    from agent.skills.article_triage import register as _reg_article_triage
    from agent.skills.draft_revision import register as _reg_draft_revision
    from agent.skills.draft_writing import register as _reg_draft_writing
    from agent.skills.full_draft_workflow import register as _reg_full_draft_workflow
    from agent.skills.product_scoring import register as _reg_product_scoring

    for register in (
        _reg_article_triage,
        _reg_product_scoring,
        _reg_draft_writing,
        _reg_draft_revision,
        _reg_full_draft_workflow,
    ):
        register(registry)
    return registry


def build_skill_runtime(
    *,
    registry: ExecutableSkillRegistry,
    business_executor: BusinessToolExecutor,
    artifact_store: Any,
    default_adapter: str = "production",
    trace_emitter: Any | None = None,
) -> SkillRuntime:
    return SkillRuntime(
        registry=registry,
        tool_executor=business_executor,
        artifact_store=artifact_store,
        trace_emitter=trace_emitter,
        default_adapter=default_adapter,
    )


def build_orchestration_runtime(
    *,
    skill_runtime: SkillRuntime,
    scopes: frozenset[str] | None = None,
    trace_emitter: Any | None = None,
    # Final Closure（EPIC-A / EPIC-B）
    run_store: Any | None = None,
    skill_snapshot_hash: str = "",
    wiki_version: str = "",
    task_id: str = "",
    reviewer: Any | None = None,
) -> OrchestrationRuntime:
    return OrchestrationRuntime(
        skill_runtime=skill_runtime,
        scopes=scopes,
        trace_emitter=trace_emitter,
        default_intent="full_workflow",
        run_store=run_store,
        skill_snapshot_hash=skill_snapshot_hash,
        wiki_version=wiki_version,
        task_id=task_id,
        reviewer=reviewer,
    )


def build_execution_runtime(
    *,
    settings: Any,
    business_registry: BusinessToolRegistry | None = None,
    business_executor: BusinessToolExecutor | None = None,
    artifact_store: Any | None = None,
    legacy_executor: WorkflowExecutor | None = None,
    trace_emitter: Any | None = None,
) -> ExecutionRuntimeBundle:
    """装配统一 Execution 运行时。

    Args:
        settings: Settings（须含 AGENT_EXECUTION_MODE 等字段）。
        business_registry/executor: 提供时构建 skill 栈；缺省则该次装配仅 legacy。
        artifact_store: 产物存储；缺省新建内存 ArtifactStore。
        legacy_executor: 由装配侧（worker）注入绑定了旧执行链的 LegacyPipelineExecutor。
        trace_emitter: 可选 span 发射器。
    """
    mode = settings.AGENT_EXECUTION_MODE
    business_snapshot = ""
    registry: ExecutableSkillRegistry | None = None
    skill_runtime: SkillRuntime | None = None
    orchestration_runtime: OrchestrationRuntime | None = None
    skill_executor: SkillPlannedExecutor | None = None
    shadow_executor: ShadowExecutor | None = None
    comparator: ShadowComparator | None = None
    rollout: LevelCanaryRollout | None = None
    skill_snapshot = ""

    if business_registry is not None and business_executor is not None:
        registry = build_executable_skill_registry(business_registry=business_registry)
        skill_snapshot = registry.skill_snapshot_hash()
        business_snapshot = str(business_registry.snapshot().get("fingerprint") or "")
        store = artifact_store or _new_artifact_store()

        # 生产 scope：由注册表全部 Skill 的 required_scopes 求并（最小足够集之外由调用方注入）
        scopes = _registry_scopes(registry)
        comparator = ShadowComparator()

        # 生产侧（default_adapter=production）
        prod_skill_runtime = build_skill_runtime(
            registry=registry,
            business_executor=business_executor,
            artifact_store=store,
            default_adapter="production",
            trace_emitter=trace_emitter,
        )
        skill_runtime = prod_skill_runtime
        orchestration_runtime = build_orchestration_runtime(
            skill_runtime=prod_skill_runtime,
            scopes=scopes,
            trace_emitter=trace_emitter,
        )
        skill_executor = SkillPlannedExecutor(
            orchestration_runtime=orchestration_runtime,
            result_adapter=ExecutionResultAdapter(),
            shadow=False,
        )

        # 只读侧（shadow，default_adapter=production_readonly → 拒绝写工具）
        if legacy_executor is not None and mode in ("skill_shadow",):
            readonly_skill_runtime = build_skill_runtime(
                registry=registry,
                business_executor=business_executor,
                artifact_store=store,
                default_adapter="production_readonly",
                trace_emitter=trace_emitter,
            )
            readonly_orch = build_orchestration_runtime(
                skill_runtime=readonly_skill_runtime,
                scopes=scopes,
                trace_emitter=trace_emitter,
            )
            shadow_skill = SkillPlannedExecutor(
                orchestration_runtime=readonly_orch,
                result_adapter=ExecutionResultAdapter(),
                shadow=True,
            )
            sampler = ShadowSampler(
                sample_percent=getattr(settings, "AGENT_SHADOW_SAMPLE_PERCENT", 100)
            )
            shadow_executor = ShadowExecutor(
                legacy=legacy_executor,
                skill=shadow_skill,
                comparator=comparator,
                sampler=sampler,
                metrics=ExecutionMetricsClient(),
                shadow_timeout_seconds=float(getattr(settings, "AGENT_SHADOW_TIMEOUT_SECONDS", 60)),
            )

        # Canary
        rollout = LevelCanaryRollout(
            percent=getattr(settings, "AGENT_SKILL_CANARY_PERCENT", 0),
            seed=getattr(settings, "AGENT_CANARY_HASH_SEED", "seccontent-agent-v1"),
        )
    else:
        mode = settings.AGENT_EXECUTION_MODE
        logger.warning("business_registry/executor 未提供，仅装配 legacy 执行（mode=%s）", mode)

    router = ExecutionRouter(
        mode=mode,
        legacy=legacy_executor,
        skill=skill_executor,
        shadow=shadow_executor,
        rollout=rollout,
        metrics=ExecutionMetricsClient(),
    )

    return ExecutionRuntimeBundle(
        execution_router=router,
        orchestration_runtime=orchestration_runtime,
        skill_runtime=skill_runtime,
        skill_registry=registry,
        business_tool_executor=business_executor,
        legacy_executor=legacy_executor,
        shadow_executor=shadow_executor,
        shadow_comparator=comparator,
        rollout=rollout,
        mode=mode,
        skill_snapshot_hash=skill_snapshot,
        knowledge_backend=getattr(settings, "KNOWLEDGE_BACKEND", ""),
        wiki_version="",
        business_tool_snapshot=business_snapshot,
        legacy_loaded=legacy_executor is not None,
        metadata={"engine": "skill_planned" if skill_executor is not None else "legacy"},
    )


def _new_artifact_store() -> Any:
    from agent.artifacts.store import ArtifactStore

    return ArtifactStore()


__all__ = [
    "build_executable_skill_registry",
    "build_execution_runtime",
    "build_orchestration_runtime",
    "build_skill_runtime",
]
