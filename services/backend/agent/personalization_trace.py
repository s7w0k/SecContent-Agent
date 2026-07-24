"""PersonalizationTraceService：生成快照与结果归因。

在调用 LLM 前创建 GenerationRun（status=running），
生成后更新成功/失败状态、耗时、Token 和审核结果。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from models.memory import MemoryPack
from models.personalization import (
    ExperimentInfo,
    GenerationOutcome,
    GenerationRun,
    GenerationStatus,
    MemoryPackSnapshot,
    ReviewSummary,
)

logger = logging.getLogger("backend.agent.personalization_trace")


class PersonalizationTraceService:
    """生成快照与结果归因服务。"""

    def __init__(self, db: Any):
        self.db = db

    async def create_run(
        self,
        user_id: str,
        article_url_hash: str,
        draft_index: int,
        stage: str = "draft",
        category_v2: str | None = None,
        template_id: str | None = None,
        template_key: str | None = None,
        template_version: int | None = None,
        memory_pack: MemoryPack | None = None,
        system_prompt: str = "",
        custom_prompt_version: int | None = None,
        reference_template: str | None = None,
        model_name: str = "",
        task_id: str = "",
        trace_id: str = "",
    ) -> str:
        """创建生成快照（在调用 LLM 前）。

        Returns:
            generation_id
        """
        generation_id = f"gen-{uuid4().hex[:12]}"
        now = datetime.now(UTC)

        # 计算 Prompt 哈希（不存储完整 Prompt）
        prompt_hash = ""
        if system_prompt:
            prompt_hash = f"sha256:{hashlib.sha256(system_prompt.encode()).hexdigest()[:16]}"

        ref_hash = ""
        if reference_template:
            ref_hash = f"sha256:{hashlib.sha256(reference_template.encode()).hexdigest()[:16]}"

        # Memory Pack 快照
        pack_snapshot = MemoryPackSnapshot()
        policy_version = None
        memory_summary_version = None
        memory_item_ids: list[str] = []

        if memory_pack:
            pack_snapshot = MemoryPackSnapshot(
                hard_preferences=memory_pack.hard_preferences,
                soft_preferences=[
                    {"memory_id": p.memory_id, "text": p.text, "confidence": p.confidence}
                    for p in memory_pack.soft_preferences
                ],
                avoid_patterns=memory_pack.avoid_patterns,
                rendered_char_count=memory_pack.char_count,
            )
            memory_item_ids = [item.memory_id for item in memory_pack.memory_items]
            if memory_pack.policy:
                policy_version = memory_pack.policy.version

        run = GenerationRun(
            generation_id=generation_id,
            trace_id=trace_id,
            task_id=task_id,
            user_id=user_id,
            article_url_hash=article_url_hash,
            draft_index=draft_index,
            stage=stage,
            category_v2=category_v2,
            template_id=template_id,
            template_key=template_key,
            template_version=template_version,
            profile_policy_version=policy_version,
            memory_summary_version=memory_summary_version,
            memory_item_ids=memory_item_ids,
            memory_pack_snapshot=pack_snapshot,
            system_prompt_version="draft-v2",
            system_prompt_hash=prompt_hash,
            custom_prompt_version=custom_prompt_version,
            reference_template_hash=ref_hash,
            model_name=model_name,
            experiment=ExperimentInfo(),
            generation_status=GenerationStatus.RUNNING,
        )

        await self.db["generation_runs"].insert_one(run.model_dump(mode="python"))
        logger.info("generation run created: generation_id=%s", generation_id)
        return generation_id

    async def complete_run(
        self,
        generation_id: str,
        success: bool = True,
        input_tokens: int = 0,
        output_tokens: int = 0,
        duration_ms: int = 0,
        error: str | None = None,
    ) -> None:
        """更新生成结果（生成完成后）。"""
        status = GenerationStatus.COMPLETED if success else GenerationStatus.FAILED
        update: dict[str, Any] = {
            "generation_status": status.value,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": duration_ms,
            "updated_at": datetime.now(UTC),
        }
        if error:
            update["error"] = error[:500]

        await self.db["generation_runs"].update_one(
            {"generation_id": generation_id},
            {"$set": update},
        )
        logger.info("generation run completed: %s status=%s", generation_id, status.value)

    async def update_review(
        self,
        generation_id: str,
        review_status: str,
        high: int = 0,
        medium: int = 0,
        low: int = 0,
    ) -> None:
        """更新审核结果。"""
        await self.db["generation_runs"].update_one(
            {"generation_id": generation_id},
            {"$set": {
                "review": ReviewSummary(
                    status=review_status, high=high, medium=medium, low=low,
                ).model_dump(),
                "updated_at": datetime.now(UTC),
            }},
        )

    async def record_outcome(
        self,
        generation_id: str,
        outcome_type: str,
        value: Any = None,
    ) -> None:
        """记录用户行为结果回流。

        Args:
            generation_id: 生成 ID
            outcome_type: viewed/downloaded/feedback_rating/revision_requested/revision_applied/personalization_feedback
            value: 值
        """
        field_map = {
            "viewed": "outcomes.viewed",
            "downloaded": "outcomes.downloaded",
            "feedback_rating": "outcomes.feedback_rating",
            "revision_requested": "outcomes.revision_requested",
            "revision_applied": "outcomes.revision_applied",
            "personalization_feedback": "outcomes.personalization_feedback",
        }

        field = field_map.get(outcome_type)
        if not field:
            return

        await self.db["generation_runs"].update_one(
            {"generation_id": generation_id},
            {"$set": {field: value if value is not None else True, "updated_at": datetime.now(UTC)}},
        )
        logger.info("outcome recorded: generation_id=%s type=%s", generation_id, outcome_type)
