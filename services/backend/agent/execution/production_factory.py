"""ProductionRuntimeBuilder - 生产唯一装配入口（OneShot Cutover 计划 §5 / §6 / §7 / §8）。

main.py 与 worker.py 必须共用本 Builder（§20 / §21 / §95），禁止两侧各自复制装配逻辑。

装配规则（§7 / §8）：

    mode           = settings.AGENT_EXECUTION_MODE      # 直接访问，禁止 getattr
    need_legacy    = mode in {"legacy", "skill_shadow", "skill_canary"}
    need_skill     = mode in {"skill_shadow", "skill_canary", "skill_planned"}

    | Mode           | Legacy | Skill    | Shadow | Canary |
    |----------------|:------:|:--------:|:------:|:------:|
    | legacy         |  ✅    |  ❌       |  ❌     |  ❌     |
    | skill_shadow   |  ✅    |  ✅ readonly| ✅    |  ❌    |
    | skill_canary   |  ✅    |  ✅      |  ❌     |  ✅    |
    | skill_planned  |  ❌    |  ✅      |  ❌     |  ❌     |

不变量（§75 / §98）：Skill 失败绝不自动 fallback Legacy。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.artifacts.mongo_store import MongoArtifactStore
from agent.business_tools.contracts import BusinessToolRegistry
from agent.business_tools.execution import BusinessToolExecutor
from agent.execution.business_runtime_factory import build_business_runtime
from agent.execution.comparator import ShadowComparator
from agent.execution.contracts import WorkflowExecutor
from agent.execution.legacy_runtime import build_legacy_runtime
from agent.execution.metrics import ExecutionMetricsClient
from agent.execution.rollout import LevelCanaryRollout, ShadowSampler
from agent.execution.router import ExecutionRouter
from agent.execution.run_store import ExecutionRunStore
from agent.execution.shadow_executor import ShadowExecutor
from agent.execution.skill_executor import SkillPlannedExecutor
from agent.execution.skill_runtime_bundle import build_skill_runtime_bundle
from agent.orchestration.runtime import OrchestrationRuntime
from agent.skills.runtime import SkillRuntime


@dataclass
class ProductionExecutionRuntime:
    """生产统一 Execution 运行时契约（§6）。"""

    execution_router: ExecutionRouter

    skill_runtime: SkillRuntime | None
    orchestration_runtime: OrchestrationRuntime | None

    business_registry: BusinessToolRegistry | None
    business_executor: BusinessToolExecutor | None

    artifact_store: MongoArtifactStore | None

    legacy_executor: WorkflowExecutor | None
    skill_executor: SkillPlannedExecutor | None
    shadow_executor: ShadowExecutor | None
    rollout: LevelCanaryRollout | None

    mode: str

    skill_snapshot_hash: str
    business_tool_snapshot: str

    knowledge_backend: str
    wiki_version: str

    legacy_loaded: bool
    skill_loaded: bool


def build_production_execution_runtime(
    *,
    settings: Any,
    db: Any,
    llm: Any = None,
    knowledge_loader: Any = None,
    knowledge_runtime: Any = None,
    crawl_client: Any = None,
    search_client: Any = None,
    template_repository: Any = None,
    classifier_v2: Any = None,
    scorer_v2: Any = None,
    draft_gen: Any = None,
    draft_reviewer: Any = None,
    draft_chat: Any = None,
    legacy_executor: WorkflowExecutor | None = None,
    product_catalog: Any = None,
    product_matcher: Any = None,
    review_policy: Any | None = None,
) -> ProductionExecutionRuntime:
    """装配生产 Execution 运行时。main 与 worker 统一调用（§20 / §21）。

    Args:
        settings: Settings（须含 AGENT_EXECUTION_MODE 等字段）。
        db: Mongo 数据库句柄。
        llm / knowledge_loader / knowledge_runtime: 基础依赖（§20）。
        crawl_client / search_client: 数据源客户端。
        classifier_v2 / scorer_v2 / draft_gen / draft_reviewer / draft_chat: 业务服务
            依赖，用于构建 ProductionBusinessToolService（§14）。need_skill 时必须提供。
        legacy_executor: 由 worker 注入的旧链执行器（§9 / §122）；main 不注入。
        template_repository / product_catalog / product_matcher: 可选业务依赖。
        review_policy: 可选 DraftReviewPolicy（EPIC-B）。skill_planned 模式传入后
            Reviewer 进入 Draft 主链；None 时不接入（保持环境可运行）。

    Returns:
        满足对应模式装配矩阵（§8）的 ProductionExecutionRuntime。
    """
    mode = settings.AGENT_EXECUTION_MODE  # §7 直接访问，禁止 getattr
    need_legacy = mode in {"legacy", "skill_shadow", "skill_canary"}
    need_skill = mode in {"skill_shadow", "skill_canary", "skill_planned"}

    # §94：非 legacy 模式（skill_planned）绝不加载旧链执行器，即使调用方误传也强制丢弃
    if not need_legacy:
        legacy_executor = None

    # 业务 Tool Runtime（生产持久化 Artifact）——任意模式都构建，供主链 + 聊天引擎使用（§14）
    business_registry: BusinessToolRegistry | None = None
    business_executor: BusinessToolExecutor | None = None
    artifact_store: MongoArtifactStore | None = None
    business_snapshot = ""
    business_runtime_obj = build_business_runtime(
        db=db,
        classifier=classifier_v2,
        scorer=scorer_v2,
        draft_generator=draft_gen,
        draft_reviewer=draft_reviewer,
        draft_chat=draft_chat,
        crawl_client=crawl_client,
        search_client=search_client,
        product_catalog=product_catalog,
        product_matcher=product_matcher,
    )
    business_registry = business_runtime_obj.business_registry
    business_executor = business_runtime_obj.business_executor
    if db is not None:
        artifact_store = business_runtime_obj.artifact_store
    business_snapshot = str(business_registry.snapshot().get("fingerprint") or "")

    # Legacy Runtime（§9 / §22）：skill_planned 不构造（§94）
    legacy_bundle = build_legacy_runtime(executor=legacy_executor) if need_legacy else None
    _ = legacy_bundle  # 保留句柄便于扩展

    # Skill Runtime（§12 / §13）：仅 need_skill 时装配（§8 矩阵）
    skill_executor: SkillPlannedExecutor | None = None
    skill_runtime: SkillRuntime | None = None
    orchestration_runtime: OrchestrationRuntime | None = None
    skill_snapshot = ""
    shadow_executor: ShadowExecutor | None = None
    run_store: Any | None = None
    if need_skill and business_registry is not None and business_executor is not None and artifact_store is not None:
        # Durable Resume（EPIC-A §5 / §8）：持久化执行步进，skill_planned 可幂等恢复。
        run_store = ExecutionRunStore(db) if db is not None else None
        default_adapter = "production_readonly" if mode == "skill_shadow" else "production"
        skill_bundle = build_skill_runtime_bundle(
            business_registry=business_registry,
            business_executor=business_executor,
            artifact_store=artifact_store,
            default_adapter=default_adapter,
            trace_emitter=None,
            run_store=run_store,
            reviewer=review_policy,
        )
        skill_executor = skill_bundle.skill_executor
        skill_runtime = skill_bundle.skill_runtime
        orchestration_runtime = skill_bundle.orchestration_runtime
        skill_snapshot = skill_bundle.skill_registry.skill_snapshot_hash()

        # Shadow（§25 / §26 / §29）：Shadow 使用只读 adapter，正式结果永远来自 Legacy
        if mode == "skill_shadow" and legacy_executor is not None:
            shadow_sampler = ShadowSampler(
                sample_percent=int(settings.AGENT_SHADOW_SAMPLE_PERCENT)
            )
            shadow_comparator = ShadowComparator()
            shadow_executor = ShadowExecutor(
                legacy=legacy_executor,
                skill=skill_executor,
                comparator=shadow_comparator,
                sampler=shadow_sampler,
                metrics=ExecutionMetricsClient(),
                shadow_timeout_seconds=float(settings.AGENT_SHADOW_TIMEOUT_SECONDS),
            )

    # Canary（§30 / §31）
    rollout: LevelCanaryRollout | None = None
    if mode == "skill_canary":
        rollout = LevelCanaryRollout(
            percent=int(settings.AGENT_SKILL_CANARY_PERCENT),
            seed=getattr(settings, "AGENT_CANARY_HASH_SEED", "seccontent-agent-v1"),
        )

    # ExecutionRouter 统一分发（§29 / §30）
    router = ExecutionRouter(
        mode=mode,
        legacy=legacy_executor,
        skill=skill_executor,
        shadow=shadow_executor,
        rollout=rollout,
        metrics=ExecutionMetricsClient(),
    )

    knowledge_backend = getattr(settings, "KNOWLEDGE_BACKEND", "wiki")
    wiki_version = ""
    if knowledge_runtime is not None:
        wiki_version = str(getattr(knowledge_runtime, "active_version", "") or "")

    return ProductionExecutionRuntime(
        execution_router=router,
        skill_runtime=skill_runtime,
        orchestration_runtime=orchestration_runtime,
        business_registry=business_registry,
        business_executor=business_executor,
        artifact_store=artifact_store,
        legacy_executor=legacy_executor,
        skill_executor=skill_executor,
        shadow_executor=shadow_executor,
        rollout=rollout,
        mode=mode,
        skill_snapshot_hash=skill_snapshot,
        business_tool_snapshot=business_snapshot,
        knowledge_backend=knowledge_backend,
        wiki_version=wiki_version,
        legacy_loaded=legacy_executor is not None,
        skill_loaded=skill_executor is not None,
    )


__all__ = ["ProductionExecutionRuntime", "build_production_execution_runtime"]
