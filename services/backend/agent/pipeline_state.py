"""MongoDB-backed lifecycle state for pipeline tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ReturnDocument


class PipelineStateManager:
    """Persist pipeline task state without relying on process-local memory."""

    def __init__(self, db: Any):
        self._collection = db["pipeline_tasks"]

    async def create_task(
        self,
        task_id: str,
        user_id: str,
        task_type: str,
        *,
        crawl_days: int = 1,
        article_url_hash: str | None = None,
        trace_id: str = "",
        username: str = "",
    ) -> dict[str, Any]:
        """Create a task document with checkpoint-ready metadata."""
        now = datetime.now(UTC)
        document: dict[str, Any] = {
            "task_id": task_id,
            "user_id": user_id,
            "task_type": task_type,
            "article_url_hash": article_url_hash,
            "status": "pending",
            "progress": {
                "phase": "pending",
                "current": 0,
                "total": 8,
                "message": "排队中...",
            },
            "thread_id": f"thread-{task_id}",
            "checkpoint_ns": "",
            "last_node": "",
            "retry_count": 0,
            "crawl_days": crawl_days,
            "trace_id": trace_id or None,
            "username": username or user_id,
            "result": None,
            "error": None,
            "state": {},
            "created_at": now,
            "updated_at": now,
            "expires_at": now + timedelta(hours=2),
        }
        await self._collection.insert_one(document)
        return document

    async def get_task(self, task_id: str, user_id: str) -> dict[str, Any] | None:
        """Return only a task owned by ``user_id``."""
        return await self._collection.find_one({"task_id": task_id, "user_id": user_id})

    async def update_status(
        self,
        task_id: str,
        status: str,
        *,
        progress: dict[str, Any] | None = None,
        last_node: str | None = None,
        state: dict[str, Any] | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        task_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Atomically update lifecycle state for one task."""
        update: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.now(UTC),
        }
        if progress is not None:
            update["progress"] = progress
        if last_node is not None:
            update["last_node"] = last_node
        if state is not None:
            update["state"] = state
        if error is not None:
            update["error"] = error[:2000]
        if result is not None:
            update["result"] = result
        if task_metadata:
            update.update(task_metadata)
        await self._collection.update_one({"task_id": task_id}, {"$set": update})

    async def increment_retry(self, task_id: str) -> int:
        """Atomically increment and return the retry count."""
        document = await self._collection.find_one_and_update(
            {"task_id": task_id},
            {
                "$inc": {"retry_count": 1},
                "$set": {"updated_at": datetime.now(UTC)},
            },
            return_document=ReturnDocument.AFTER,
        )
        return int(document.get("retry_count", 0)) if document else 0
