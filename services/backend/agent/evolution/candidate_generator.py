"""候选策略生成器。

允许演进的目标：memory_renderer 模板、retrieval_weights、template_ranker 权重。
禁止自动修改安全 Prompt、产品知识、API 权限、数据库迁移、代码。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger("backend.agent.evolution.candidate_generator")


# 可演进目标类型
EVOLVABLE_TARGETS = {
    "memory_renderer": "Memory Pack 渲染模板",
    "retrieval_weights": "检索权重配置",
    "template_ranker": "模板排序权重",
    "conflict_config": "冲突处理配置",
}

# 当前基线版本
BASE_VERSIONS = {
    "memory_renderer": "memory-renderer-v1",
    "retrieval_weights": "retrieval-weights-v1",
    "template_ranker": "template-ranker-v1",
    "conflict_config": "conflict-config-v1",
}


class CandidateGenerator:
    """生成候选策略。"""

    def __init__(self, db: Any):
        self.db = db

    async def generate(
        self,
        target_type: str,
        source_dataset_id: str,
        content: dict | None = None,
        *,
        baseline_snapshot_id: str,
        hypothesis: str,
        target_failures: list[str],
        expected_metrics: dict[str, float],
    ) -> dict:
        """生成一个候选策略。

        Args:
            target_type: 演进目标类型
            source_dataset_id: 数据集 ID
            content: 候选内容（模板/权重/配置）

        Returns:
            候选文档
        """
        if target_type not in EVOLVABLE_TARGETS:
            raise ValueError(f"Unsupported target type: {target_type}")
        if not source_dataset_id or not baseline_snapshot_id:
            raise ValueError("candidate requires dataset and baseline snapshot")
        if len(hypothesis.strip()) < 12 or not target_failures or not expected_metrics:
            raise ValueError("candidate requires an improvement hypothesis, failures and metrics")

        base_version = BASE_VERSIONS.get(target_type, "v1")
        candidate_id = f"pcand-{uuid4().hex[:8]}"
        candidate_version = f"{base_version}-c{uuid4().hex[:4]}"

        candidate_doc = {
            "candidate_id": candidate_id,
            "target_type": target_type,
            "base_version": base_version,
            "candidate_version": candidate_version,
            "content": content or {},
            "source_dataset_id": source_dataset_id,
            "baseline_snapshot_id": baseline_snapshot_id,
            "hypothesis": hypothesis.strip(),
            "target_failures": list(target_failures),
            "expected_metrics": dict(expected_metrics),
            "registry": "draft",
            "status": "draft",
            "metrics": {},
            "created_by": "offline-evaluator",
            "approved_by": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

        await self.db["personalization_candidates"].insert_one(candidate_doc)
        logger.info("candidate generated: id=%s target=%s", candidate_id, target_type)
        return candidate_doc
