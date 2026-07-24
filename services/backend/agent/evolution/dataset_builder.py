"""离线评测数据集构建器。

从 generation_runs 构造去标识化样本，按分类分层抽样，
划分训练/验证/留出集，同一用户同一文章的修订链必须在同一分区。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger("backend.agent.evolution.dataset_builder")


class DatasetBuilder:
    """从 generation_runs 构造去标识化评测集。"""

    def __init__(self, db: Any):
        self.db = db

    async def build(
        self,
        days: int = 90,
        train_ratio: float = 0.6,
        val_ratio: float = 0.2,
    ) -> dict:
        """构建评测数据集。

        Args:
            days: 回溯天数
            train_ratio: 训练集比例
            val_ratio: 验证集比例（剩余为留出集）

        Returns:
            {"dataset_id": str, "total": int, "splits": dict}
        """
        cutoff = datetime.now(UTC).timestamp() - days * 86400
        cursor = self.db["generation_runs"].find({
            "generation_status": "completed",
        })
        all_runs = await cursor.to_list(length=10000)

        # 按 (user_id, article_url_hash) 分组修订链
        chains: dict[str, list[dict]] = {}
        for run in all_runs:
            created = run.get("created_at")
            if isinstance(created, datetime) and created.timestamp() < cutoff:
                continue
            key = f"{run.get('user_id', '')}:{run.get('article_url_hash', '')}"
            chains.setdefault(key, []).append(self._deidentify(run))

        # 按分类分层抽样
        by_category: dict[str, list[str]] = {}
        for key, samples in chains.items():
            cat = samples[0].get("category_v2", "unknown") or "unknown"
            by_category.setdefault(cat, []).append(key)

        # 分区
        splits = {"train": [], "val": [], "holdout": []}
        import random

        for _cat, keys in by_category.items():
            random.shuffle(keys)
            n = len(keys)
            train_end = int(n * train_ratio)
            val_end = int(n * (train_ratio + val_ratio))
            for key in keys[:train_end]:
                splits["train"].extend(chains[key])
            for key in keys[train_end:val_end]:
                splits["val"].extend(chains[key])
            for key in keys[val_end:]:
                splits["holdout"].extend(chains[key])

        dataset_id = f"dataset-{datetime.now(UTC).strftime('%Y%m%d')}-{uuid4().hex[:6]}"

        # 存储
        dataset_doc = {
            "dataset_id": dataset_id,
            "created_at": datetime.now(UTC),
            "days": days,
            "total_samples": sum(len(s) for s in splits.values()),
            "splits": {k: len(v) for k, v in splits.items()},
            "categories": {k: len(v) for k, v in by_category.items()},
        }
        await self.db["personalization_datasets"].insert_one(dataset_doc)

        # 存储样本
        for split_name, samples in splits.items():
            for sample in samples:
                sample["dataset_id"] = dataset_id
                sample["split"] = split_name
                await self.db["personalization_samples"].insert_one(sample)

        logger.info(
            "dataset built: id=%s total=%d train=%d val=%d holdout=%d",
            dataset_id, dataset_doc["total_samples"],
            len(splits["train"]), len(splits["val"]), len(splits["holdout"]),
        )

        return {
            "dataset_id": dataset_id,
            "total": dataset_doc["total_samples"],
            "splits": dataset_doc["splits"],
            "categories": dataset_doc["categories"],
        }

    def _deidentify(self, run: dict) -> dict:
        """去标识化处理。"""
        return {
            "sample_id": f"sample-{uuid4().hex[:8]}",
            "category_v2": run.get("category_v2"),
            "template_key": run.get("template_key"),
            "memory_pack": run.get("memory_pack_snapshot", {}),
            "review": run.get("review", {}),
            "outcomes": run.get("outcomes", {}),
            "experiment_group": run.get("experiment", {}).get("group", "control"),
        }
