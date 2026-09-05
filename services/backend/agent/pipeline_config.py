"""流水线配置冻结服务。

在 API 创建任务时：
1. 合并单次选项、用户默认和系统默认
2. 校验产品选择
3. 解析并冻结 PromptRef
4. 解析产品目录版本
5. 计算 config_fingerprint
6. 生成 PipelineConfigSnapshot

Worker 只读取任务快照，不重新解释账号默认偏好。
"""

from __future__ import annotations

import logging
from typing import Any

from agent.knowledge_slice import KnowledgeSliceResolver
from agent.product_catalog import ProductCatalogService
from agent.product_matcher import ProductMatcher
from agent.product_routing import ProductRoutingService
from agent.prompt_resolver import PromptResolver
from models.generation_config import (
    GenerationOptions,
    PipelineConfigSnapshot,
    ProductTargetMode,
    ScoreMode,
    compute_config_fingerprint,
    merge_options_with_preferences,
)

logger = logging.getLogger("backend.pipeline_config")


class PipelineConfigFreezer:
    """流水线配置冻结器。"""

    def __init__(
        self,
        resolver: PromptResolver,
        catalog: ProductCatalogService | None = None,
        slice_resolver: KnowledgeSliceResolver | None = None,
        matcher: ProductMatcher | None = None,
        db=None,
    ):
        self._resolver = resolver
        self._catalog = catalog or ProductCatalogService()
        self._slice_resolver = slice_resolver
        self._matcher = matcher or ProductMatcher(self._catalog)
        self._routing_service = ProductRoutingService(
            catalog=self._catalog,
            matcher=self._matcher,
        )
        self._db = db

    async def freeze(
        self,
        user_id: str,
        options: GenerationOptions | None = None,
        *,
        prompt_keys: list[str] | None = None,
        article: dict[str, Any] | None = None,
    ) -> PipelineConfigSnapshot:
        """冻结配置快照。

        Args:
            user_id: 用户 ID
            options: 单次请求覆盖选项
            prompt_keys: 需要冻结的提示词键列表
            article: 文章数据（auto 模式匹配产品时使用）

        Returns:
            PipelineConfigSnapshot
        """
        # 1. 获取用户偏好
        from api.generation_preferences import _get_preferences

        prefs = await _get_preferences(self._resolver._db, user_id)

        # 2. 合并选项
        relevance, mode, product_ids = merge_options_with_preferences(options, prefs)

        # 3. 确定 score_mode
        score_mode = ScoreMode.PRODUCT_EVENT if relevance else ScoreMode.EVENT_ONLY

        # 4. 冻结产品路由（阶段2 S2-1：selected/auto/none 统一经路由服务解析）
        resolved_products: list[dict[str, Any]] = []
        knowledge_hash = ""
        knowledge_source_ids: list[str] = []
        knowledge_fallback: str | None = None
        routing = None

        if mode == ProductTargetMode.NONE:
            routing = await self._routing_service.resolve(article or {}, mode, [], user_id)
            knowledge_hash = "sha256:none"
        elif mode == ProductTargetMode.SELECTED:
            routing = await self._routing_service.resolve(article or {}, mode, product_ids, user_id)
            resolved_products = [rp.model_dump() for rp in routing.resolved_products]
            knowledge_hash, knowledge_source_ids = await self._resolve_knowledge(
                "score",
                [rp.product_id for rp in routing.resolved_products],
                user_id,
            )
        elif mode == ProductTargetMode.AUTO:
            user_products = None
            if self._db:
                from agent.knowledge_merger import KnowledgeMerger

                merger = KnowledgeMerger(self._db)
                user_products = await merger.get_user_products_for_matching(user_id)

            routing = await self._routing_service.resolve(
                article or {},
                mode,
                [],
                user_id,
                user_products=user_products,
            )
            resolved_products = [rp.model_dump() for rp in routing.resolved_products]
            matched_ids = [rp.product_id for rp in routing.resolved_products]
            if matched_ids:
                knowledge_hash, knowledge_source_ids = await self._resolve_knowledge(
                    "score", matched_ids, user_id
                )
            else:
                knowledge_hash = self._catalog.catalog_hash()
                knowledge_fallback = "product_unresolved"

        # 5. 冻结 PromptRef
        default_prompt_keys = prompt_keys or [
            "classify_v2_business",
            "score_v2_business",
            "draft_generation_business",
        ]
        prompt_refs = await self._resolver.freeze_many(user_id, default_prompt_keys)

        # 6. 计算配置指纹
        config_fingerprint = compute_config_fingerprint(
            product_relevance_enabled=relevance,
            product_target_mode=mode.value,
            selected_product_ids=[p["product_id"] for p in resolved_products],
            knowledge_hash=knowledge_hash,
            prompt_refs=[ref.model_dump() for ref in prompt_refs],
        )

        snapshot = PipelineConfigSnapshot(
            schema_version=1,
            product_relevance_enabled=relevance,
            score_mode=score_mode,
            product_target_mode=mode,
            selected_product_ids=[p["product_id"] for p in resolved_products],
            resolved_products=resolved_products,
            prompt_refs=prompt_refs,
            knowledge_hash=knowledge_hash,
            config_fingerprint=config_fingerprint,
            force_generate=options.force_generate if options else False,
            # 阶段2：冻结路由快照与知识元数据
            routing=routing,
            routing_version=routing.routing_version if routing else "",
            knowledge_source_ids=knowledge_source_ids,
            knowledge_fallback=knowledge_fallback,
        )

        logger.info(
            "Config frozen: user=%s mode=%s score_mode=%s products=%d fingerprint=%s",
            user_id,
            mode.value,
            score_mode.value,
            len(resolved_products),
            config_fingerprint[:24],
        )

        return snapshot

    async def _resolve_knowledge(
        self,
        purpose: str,
        product_ids: list[str],
        user_id: str,
    ) -> tuple[str, list[str]]:
        """按产品解析知识哈希与来源 ID 列表。

        Returns:
            (knowledge_hash, knowledge_source_ids)
        """
        if product_ids and self._slice_resolver:
            slice_result = await self._slice_resolver.resolve(
                purpose=purpose,
                product_ids=product_ids,
                include_shared=False,
                user_id=user_id if self._db else None,
            )
            return slice_result.content_hash, list(slice_result.source_document_ids)
        return self._catalog.catalog_hash(), []
