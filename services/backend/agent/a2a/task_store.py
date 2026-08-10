"""A2ATaskStore — 阶段四 4B Step 4B-1。

A2A Task 的 Mongo 持久化与幂等保证：
  - a2a_tasks 集合：id（task_id）唯一；user_id+created_timestamp、context_id、
    internal_run_id、status+last_updated_timestamp 索引（a2a_task_id <-> internal_run_id 双向索引）；
  - 版本乐观锁：save / update_status 携带 expected_version，不匹配拒绝旧版本覆盖
    （最终一致性：状态变更带版本号，旧写入不覆盖新状态）；
  - 幂等：重复创建按 task_id 去重返回 False；状态更新重复应用被版本号拒绝；
    事件投递去重走 a2a_event_ledger（task_id + event_id 唯一），重复订阅/推送不重复投递；
  - 多租户：所有读取 / 更新按 user_id 过滤；
  - 终态不可逆：终态 Task 拒绝任何状态迁移（与 TERMINAL_TASK_STATUSES 一致）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from agent.a2a.models import TERMINAL_TASK_STATUSES, A2AError, Task, TaskStatus
from pymongo import ASCENDING, DESCENDING, IndexModel

logger = logging.getLogger("backend.agent.a2a_task_store")

COLLECTION = "a2a_tasks"
LEDGER_COLLECTION = "a2a_event_ledger"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class A2ATaskConflictError(A2AError):
    """版本冲突 / 终态不可逆 / 归属校验失败。"""


class A2ATaskStore:
    """A2A Task 持久化（a2a_tasks + a2a_event_ledger）。"""

    def __init__(
        self,
        db: Any,
        *,
        collection: str = COLLECTION,
        ledger_collection: str = LEDGER_COLLECTION,
    ):
        self.db = db
        self.collection_name = collection
        self.ledger_collection_name = ledger_collection
        self.col = db[collection]
        self.ledger = db[ledger_collection]

    def index_specs(self) -> dict[str, list[IndexModel]]:
        """a2a_tasks 集合索引（与 db/mongo.py ensure_indexes 保持一致）。"""
        return {
            self.collection_name: [
                IndexModel([("id", ASCENDING)], unique=True, name="uq_a2a_task_id"),
                IndexModel(
                    [("user_id", ASCENDING), ("created_timestamp", DESCENDING)],
                    name="idx_a2a_task_user_created",
                ),
                IndexModel(
                    [("internal_run_id", ASCENDING)],
                    sparse=True,
                    name="idx_a2a_task_run_id",
                ),
                IndexModel(
                    [("context_id", ASCENDING)],
                    sparse=True,
                    name="idx_a2a_task_context_id",
                ),
                IndexModel(
                    [("status", ASCENDING), ("last_updated_timestamp", DESCENDING)],
                    name="idx_a2a_task_status_updated",
                ),
            ],
            self.ledger_collection_name: [
                IndexModel(
                    [("task_id", ASCENDING), ("event_id", ASCENDING)],
                    unique=True,
                    name="uq_a2a_ledger_task_event",
                ),
            ],
        }

    async def ensure_indexes(self) -> list[str]:
        created: list[str] = []
        for name, specs in self.index_specs().items():
            created.extend(await self.db[name].create_indexes(specs))
        return created

    # ── 创建 / 保存（版本乐观锁） ──────────────────────────

    async def create(self, task: Task, *, user_id: str, now: datetime | None = None) -> bool:
        """幂等创建：task_id 已存在返回 False，不覆盖既有状态。"""
        stamp = now or _utc_now()
        existing = await self.col.find_one({"id": task.id})
        if existing is not None:
            return False
        doc = self._task_to_doc(task, user_id=user_id, version=1, stamp=stamp)
        await self.col.insert_one(doc)
        return True

    async def save(
        self,
        task: Task,
        *,
        user_id: str,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """保存（可新建或更新）。expected_version 提供时执行 CAS：

        - 版本不匹配返回 False（拒绝旧版本覆盖新状态）；
        - 终态 Task 拒绝保存为非终态。
        """
        stamp = now or _utc_now()
        current = await self.col.find_one({"id": task.id})
        if current is not None and current.get("user_id") != user_id:
            raise A2ATaskConflictError("task belongs to another user")
        if current is not None:
            current_status = TaskStatus(current["status"])
            if (
                current_status in TERMINAL_TASK_STATUSES
                and task.status not in TERMINAL_TASK_STATUSES
            ):
                return False  # 终态不可逆
            if (
                expected_version is not None
                and current.get("checkpoint_version", 0) != expected_version
            ):
                return False
            next_version = current.get("checkpoint_version", 0) + 1
        else:
            next_version = expected_version + 1 if expected_version is not None else 1

        doc = self._task_to_doc(task, user_id=user_id, version=next_version, stamp=stamp)
        await self.col.replace_one({"id": task.id}, doc, upsert=current is None)
        return True

    # ── 读取（多租户：user_id 过滤） ────────────────────────

    async def load(self, task_id: str, *, user_id: str = "") -> Task | None:
        query = {"id": task_id}
        if user_id:
            query["user_id"] = user_id
        doc = await self.col.find_one(query)
        return None if doc is None else self._doc_to_task(doc)

    async def load_by_run_id(self, internal_run_id: str, *, user_id: str = "") -> Task | None:
        """反向索引：internal_run_id -> A2A Task。"""
        query = {"internal_run_id": internal_run_id}
        if user_id:
            query["user_id"] = user_id
        doc = await self.col.find_one(query)
        return None if doc is None else self._doc_to_task(doc)

    async def list_tasks(
        self,
        *,
        user_id: str,
        status: str = "",
        limit: int = 50,
    ) -> list[Task]:
        """列出 Task（多租户：user_id 必填过滤）。"""
        query: dict[str, Any] = {}
        if user_id:
            query["user_id"] = user_id
        if status:
            query["status"] = status
        try:
            cursor = self.col.find(query).sort("created_timestamp", -1).limit(limit)
            docs = await cursor.to_list(length=limit)
            return [self._doc_to_task(d) for d in docs]
        except Exception:
            logger.warning("[a2a_task_store] list_tasks failed")
            return []

    # ── 状态更新（终态不可逆 + 版本守卫 + 幂等） ────────────

    async def update_status(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        user_id: str,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """状态迁移：终态拒绝再迁移；版本不符拒绝；重复应用同一状态返回 False。

        Returns:
            True = 本次确实发生了迁移；False = 幂等无操作 / 终态 / 版本冲突。
        """
        query: dict[str, Any] = {"id": task_id}
        if user_id:
            query["user_id"] = user_id
        current = await self.col.find_one(query)
        if current is None:
            return False
        current_status = TaskStatus(current["status"])
        if current_status in TERMINAL_TASK_STATUSES:
            return False
        if current_status == new_status:
            return False  # 幂等：状态未变不重复写
        if (
            expected_version is not None
            and current.get("checkpoint_version", 0) != expected_version
        ):
            return False

        stamp = now or _utc_now()
        next_version = current.get("checkpoint_version", 0) + 1
        result = await self.col.update_one(
            query,
            {
                "$set": {
                    "status": new_status.value,
                    "last_updated_timestamp": stamp,
                    "checkpoint_version": next_version,
                }
            },
        )
        return result.modified_count > 0

    # ── 事件投递去重（Subscribe / 推送幂等） ────────────────

    async def record_event(self, task_id: str, event_id: str) -> bool:
        """投递去重记账：同一 (task_id, event_id) 只允许投递一次。"""
        existing = await self.ledger.find_one({"task_id": task_id, "event_id": event_id})
        if existing is not None:
            return False
        await self.ledger.insert_one(
            {
                "task_id": task_id,
                "event_id": event_id,
                "delivered_at": _utc_now(),
            }
        )
        return True

    async def has_event(self, task_id: str, event_id: str) -> bool:
        """查询某事件是否已投递（Subscribe 游标重连去重用）。"""
        doc = await self.ledger.find_one({"task_id": task_id, "event_id": event_id})
        return doc is not None

    # ── 序列化辅助 ──────────────────────────────────────────

    @staticmethod
    def _task_to_doc(task: Task, *, user_id: str, version: int, stamp: datetime) -> dict:
        doc = task.model_dump(mode="json")
        doc["id"] = task.id
        doc["user_id"] = user_id
        doc["checkpoint_version"] = version
        doc["last_updated_timestamp"] = stamp
        return doc

    @staticmethod
    def _doc_to_task(doc: dict) -> Task:
        data = {k: v for k, v in doc.items() if k not in {"_id", "user_id", "checkpoint_version"}}
        return Task(**data)
