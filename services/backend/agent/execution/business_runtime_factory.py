"""BusinessRuntimeFactory - 业务 Tool Runtime 统一装配（OneShot Cutover 计划 §14 / §15）。

main 与 worker 复用同一个 build_business_runtime（§15），避免两侧各自复制
ProductionBusinessToolService + BusinessToolExecutor + MongoArtifactStore 装配逻辑。

装配结果（§14 / §16）：
    business_registry  -> build_business_tool_registry()
    production service -> ProductionBusinessToolService(db, classifier, scorer, ...)
    business_executor  -> BusinessToolExecutor(registry, adapters=5 kinds)
    artifact_store     -> MongoArtifactStore(db)（生产持久化，§16 / §97）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.artifacts.mongo_store import MongoArtifactStore
from agent.business_tools import (
    BusinessToolAdapterKind,
    BusinessToolExecutor,
    FakeBusinessToolAdapter,
    ProductionBusinessToolAdapter,
    ReadOnlyProductionBusinessToolAdapter,
    RecordedBusinessToolAdapter,
    SandboxBusinessToolAdapter,
    build_business_tool_registry,
)
from agent.business_tools.contracts import BusinessToolRegistry
from agent.business_tools.production import ProductionBusinessToolService


@dataclass
class BusinessRuntime:
    """业务 Tool Runtime（§14 / §15）。"""

    business_registry: BusinessToolRegistry
    business_executor: BusinessToolExecutor
    artifact_store: MongoArtifactStore
    production_service: ProductionBusinessToolService


def build_business_runtime(
    *,
    db: Any,
    classifier: Any = None,
    scorer: Any = None,
    draft_generator: Any = None,
    draft_reviewer: Any = None,
    draft_chat: Any = None,
    crawl_client: Any = None,
    search_client: Any = None,
    product_catalog: Any = None,
    product_matcher: Any = None,
) -> BusinessRuntime:
    """统一装配业务 Tool Runtime（§14 / §15）。

    Returns:
        BusinessRuntime 含 registry / executor / MongoArtifactStore / production service。
    """
    business_registry = build_business_tool_registry()

    production_tools = ProductionBusinessToolService(
        db=db,
        classifier=classifier,
        scorer=scorer,
        draft_generator=draft_generator,
        draft_reviewer=draft_reviewer,
        draft_chat=draft_chat,
        crawl_client=crawl_client,
        search_client=search_client,
        product_catalog=product_catalog,
        product_matcher=product_matcher,
    )
    business_executor = BusinessToolExecutor(
        business_registry,
        adapters={
            BusinessToolAdapterKind.FAKE: FakeBusinessToolAdapter(),
            BusinessToolAdapterKind.RECORDED: RecordedBusinessToolAdapter(),
            BusinessToolAdapterKind.SANDBOX: SandboxBusinessToolAdapter(business_registry),
            BusinessToolAdapterKind.PRODUCTION: ProductionBusinessToolAdapter(production_tools),
            BusinessToolAdapterKind.PRODUCTION_READONLY: ReadOnlyProductionBusinessToolAdapter(
                production_tools
            ),
        },
    )
    artifact_store = MongoArtifactStore(db)
    return BusinessRuntime(
        business_registry=business_registry,
        business_executor=business_executor,
        artifact_store=artifact_store,
        production_service=production_tools,
    )


__all__ = ["BusinessRuntime", "build_business_runtime"]
