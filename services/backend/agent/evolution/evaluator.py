"""候选策略评测器。

计算适应度指标和分组指标，硬门禁不进入适应度。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("backend.agent.evolution.evaluator")


class Evaluator:
    """评测候选策略。"""

    def __init__(self, db: Any):
        self.db = db

    async def evaluate(
        self,
        candidate_id: str,
        dataset_id: str,
    ) -> dict:
        """评测候选策略。

        计算训练/验证/留出集指标和分类指标。

        Returns:
            评测结果文档
        """
        candidate = await self.db["personalization_candidates"].find_one({
            "candidate_id": candidate_id,
        })
        if candidate is None:
            raise ValueError(f"Candidate not found: {candidate_id}")

        # 标记为评测中
        await self.db["personalization_candidates"].update_one(
            {"candidate_id": candidate_id},
            {"$set": {"status": "evaluating", "updated_at": datetime.now(UTC)}},
        )

        # 加载数据集样本
        cursor = self.db["personalization_samples"].find({"dataset_id": dataset_id})
        samples = await cursor.to_list(length=100000)

        # 按分区计算指标
        splits = {"train": [], "val": [], "holdout": []}
        for s in samples:
            split = s.get("split", "train")
            splits.setdefault(split, []).append(s)

        train_metrics = self._compute_metrics(splits["train"])
        val_metrics = self._compute_metrics(splits["val"])
        holdout_metrics = self._compute_metrics(splits["holdout"])

        # 分类指标
        by_category: dict[str, list[dict]] = {}
        for s in samples:
            cat = s.get("category_v2", "unknown") or "unknown"
            by_category.setdefault(cat, []).append(s)

        category_metrics = {
            cat: self._compute_metrics(cat_samples)
            for cat, cat_samples in by_category.items()
        }

        # 计算适应度
        fitness = self._compute_fitness(val_metrics)
        holdout_fitness = self._compute_fitness(holdout_metrics)

        result = {
            "dataset_id": dataset_id,
            "train_metrics": train_metrics,
            "val_metrics": val_metrics,
            "holdout_metrics": holdout_metrics,
            "category_metrics": category_metrics,
            "fitness": fitness,
            "holdout_fitness": holdout_fitness,
        }

        await self.db["personalization_candidates"].update_one(
            {"candidate_id": candidate_id},
            {"$set": {
                "status": "ready_for_review",
                "metrics": result,
                "updated_at": datetime.now(UTC),
            }},
        )

        logger.info(
            "candidate evaluated: id=%s fitness=%.4f holdout_fitness=%.4f",
            candidate_id, fitness, holdout_fitness,
        )

        return result

    def _compute_metrics(self, samples: list[dict]) -> dict[str, float]:
        """计算单组样本的指标。"""
        if not samples:
            return {}

        total = len(samples)
        downloaded = sum(1 for s in samples if s.get("outcomes", {}).get("downloaded"))
        revision_applied = sum(1 for s in samples if s.get("outcomes", {}).get("revision_applied"))
        revision_requested = sum(1 for s in samples if s.get("outcomes", {}).get("revision_requested"))
        ratings = [
            s.get("outcomes", {}).get("feedback_rating", 0)
            for s in samples
            if s.get("outcomes", {}).get("feedback_rating")
        ]

        # 审核问题
        high_issues = sum(s.get("review", {}).get("high", 0) for s in samples)

        return {
            "sample_count": total,
            "download_rate": downloaded / total,
            "revision_apply_rate": revision_applied / total,
            "revision_request_rate": revision_requested / total,
            "avg_rating": sum(ratings) / len(ratings) if ratings else 0.0,
            "high_issue_rate": high_issues / total,
        }

    def _compute_fitness(self, metrics: dict[str, float]) -> float:
        """计算适应度。

        fitness =
            0.25 × personalization_adherence
          + 0.20 × revision_apply_rate
          + 0.15 × user_rating
          + 0.15 × edit_reduction
          + 0.10 × template_match
          + 0.10 × response_quality
          + 0.05 × diversity
          - cost_penalty
          - latency_penalty
        """
        if not metrics:
            return 0.0

        # 简化版：使用可用指标
        revision_apply = metrics.get("revision_apply_rate", 0.0)
        download = metrics.get("download_rate", 0.0)
        rating = metrics.get("avg_rating", 0.0) / 5.0  # 归一化
        revision_request = metrics.get("revision_request_rate", 0.0)

        # edit_reduction: 修订请求率越低越好（1 - rate）
        edit_reduction = max(0.0, 1.0 - revision_request)

        # personalization_adherence: 下载率作为代理
        adherence = download

        fitness = (
            0.25 * adherence
            + 0.20 * revision_apply
            + 0.15 * rating
            + 0.15 * edit_reduction
            + 0.10 * download
            + 0.10 * revision_apply
            + 0.05 * min(download, 1.0)
        )

        # 惩罚
        high_issue_rate = metrics.get("high_issue_rate", 0.0)
        if high_issue_rate > 0.05:
            fitness -= 0.10 * high_issue_rate

        return round(max(0.0, min(1.0, fitness)), 4)
