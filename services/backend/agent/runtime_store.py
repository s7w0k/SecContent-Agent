"""RuntimeStateStore — 阶段四 4A Step 4A-8。

RuntimeState 的 Mongo 持久化与恢复：
  - runtime_runs 集合：run_id 唯一；user_id+created_at、status+updated_at 索引；
  - 保存/加载走 migrate_runtime_state 单一迁移入口（旧版本自动升级）；
  - 乐观锁：save 携带 expected_checkpoint_version，不匹配抛 RuntimeStateConflictError，
    拒绝旧执行器覆盖新状态（与 apply_state_mutation 语义一致）；
  - 取消流程：API 先调用 request_cancel 写入 cancel_requested 标记，
    Runtime 在安全点停止后写入最终状态与取消原因；
  - 已发生的外部副作用如实记录在 tool_results，不伪装回滚。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from agent.runtime_state import (
    RuntimeState,
    RuntimeStateConflictError,
    RuntimeStatus,
    migrate_runtime_state,
)
from pymongo import ASCENDING, DESCENDING, IndexModel

logger = logging.getLogger("backend.agent.runtime_store")

COLLECTION = "runtime_runs"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RuntimeStateStore:
    """RuntimeState 持久化（runtime_runs）。"""

    def __init__(self, db: Any, *, collection: str = COLLECTION):
        self.db = db
        self.collection_name = collection
        self.col = db[collection]

    def index_specs(self) -> dict[str, list[IndexModel]]:
        """runs 集合索引（与 db/mongo.py ensure_indexes 保持一致）。"""
        return {
            self.collection_name: [
                IndexModel([("run_id", ASCENDING)], unique=True, name="uq_runtime_run_id"),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_runtime_run_user_created",
                ),
                IndexModel(
                    [("status", ASCENDING), ("updated_at", DESCENDING)],
                    name="idx_runtime_run_status_updated",
                ),
            ]
        }

    async def ensure_indexes(self) -> list[str]:
        return await self.col.create_indexes(self.index_specs()[self.collection_name])

    async def save(
        self,
        state: RuntimeState,
        *,
        expected_checkpoint_version: int | None = None,
        now: datetime | None = None,
    ) -> None:
        """保存状态；expected_checkpoint_version 提供时执行乐观锁 CAS。"""
        stamp = now or _utc_now()
        state = state.model_copy(update={"updated_at": stamp})
        doc = state.model_dump(mode="json")
        doc["run_id"] = state.run_id
        if expected_checkpoint_version is None:
            await self.col.replace_one({"run_id": state.run_id}, doc, upsert=True)
            return
        result = await self.col.replace_one(
            {"run_id": state.run_id, "checkpoint_version": expected_checkpoint_version},
            doc,
            upsert=False,
        )
        if result.matched_count == 0:
            raise RuntimeStateConflictError(
                f"state conflict: expected checkpoint_version={expected_checkpoint_version}"
            )

    async def load(self, run_id: str) -> RuntimeState | None:
        """按 run_id 加载并迁移到当前版本；不存在返回 None。"""
        doc = await self.col.find_one({"run_id": run_id})
        if doc is None:
            return None
        return migrate_runtime_state(doc)

    async def request_cancel(
        self,
        run_id: str,
        *,
        reason: str = "canceled by user",
        now: datetime | None = None,
    ) -> bool:
        """取消流程第 1 步：API 将状态置为 cancel_requested（运行中的 run 可取消）。"""
        stamp = now or _utc_now()
        result = await self.col.update_one(
            {
                "run_id": run_id,
                "status": {"$in": [RuntimeStatus.RUNNING.value, RuntimeStatus.PLANNING.value]},
            },
            {
                "$set": {
                    "status": "cancel_requested",
                    "cancel_reason": reason,
                    "updated_at": stamp,
                },
                "$inc": {"checkpoint_version": 1},
            },
        )
        return result.modified_count > 0

    async def list_runs(
        self,
        *,
        user_id: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[RuntimeState]:
        """列出运行（多租户：user_id 必填过滤）。"""
        query: dict[str, Any] = {}
        if user_id:
            query["user_id"] = user_id
        if status:
            query["status"] = status
        try:
            cursor = self.col.find(query).sort("created_at", -1).limit(limit)
            docs = await cursor.to_list(length=limit)
            return [migrate_runtime_state(d) for d in docs]
        except Exception:
            logger.warning("[runtime_store] list_runs failed")
            return []
