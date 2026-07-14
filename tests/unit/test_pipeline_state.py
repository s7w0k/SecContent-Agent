"""Tests for MongoDB-backed V2 pipeline state."""

from __future__ import annotations

import asyncio
import os
import sys
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


class MemoryTaskCollection:
    def __init__(self):
        self.documents: dict[str, dict] = {}

    async def insert_one(self, document: dict):
        self.documents[document["task_id"]] = deepcopy(document)
        return MagicMock(inserted_id=document["task_id"])

    async def find_one(self, query: dict, **_kwargs):
        document = self.documents.get(query.get("task_id"))
        if document is None:
            return None
        if any(document.get(key) != value for key, value in query.items()):
            return None
        return deepcopy(document)

    async def update_one(self, query: dict, update: dict):
        document = self.documents.get(query["task_id"])
        if document is not None:
            document.update(deepcopy(update.get("$set", {})))
        return MagicMock(modified_count=int(document is not None))

    async def find_one_and_update(self, query: dict, update: dict, **_kwargs):
        document = self.documents.get(query["task_id"])
        if document is None:
            return None
        for key, value in update.get("$inc", {}).items():
            document[key] = document.get(key, 0) + value
        document.update(deepcopy(update.get("$set", {})))
        return deepcopy(document)


class MemoryDatabase:
    def __init__(self):
        self.tasks = MemoryTaskCollection()

    def __getitem__(self, name: str):
        assert name == "pipeline_tasks"
        return self.tasks


@pytest.mark.asyncio
async def test_create_task_contains_checkpoint_metadata():
    from agent.pipeline_state import PipelineStateManager

    db = MemoryDatabase()
    task = await PipelineStateManager(db).create_task(
        "task-a", "user-a", "run-v2", crawl_days=3, trace_id="trace-a"
    )

    assert task["status"] == "pending"
    assert task["thread_id"] == "thread-task-a"
    assert task["retry_count"] == 0
    assert task["crawl_days"] == 3


@pytest.mark.asyncio
async def test_get_task_enforces_user_isolation():
    from agent.pipeline_state import PipelineStateManager

    db = MemoryDatabase()
    manager = PipelineStateManager(db)
    await manager.create_task("task-a", "user-a", "run-v2")

    assert await manager.get_task("task-a", "user-a") is not None
    assert await manager.get_task("task-a", "user-b") is None


@pytest.mark.asyncio
async def test_update_status_persists_state_and_result():
    from agent.pipeline_state import PipelineStateManager

    db = MemoryDatabase()
    manager = PipelineStateManager(db)
    await manager.create_task("task-a", "user-a", "run-v2")
    await manager.update_status(
        "task-a",
        "completed",
        progress={"phase": "completed", "current": 4, "total": 4, "message": "done"},
        last_node="draft",
        state={"draft_count": 1},
        result={"ok": True},
    )

    task = await manager.get_task("task-a", "user-a")
    assert task is not None
    assert task["status"] == "completed"
    assert task["last_node"] == "draft"
    assert task["state"]["draft_count"] == 1
    assert task["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_increment_retry_is_persistent():
    from agent.pipeline_state import PipelineStateManager

    db = MemoryDatabase()
    manager = PipelineStateManager(db)
    await manager.create_task("task-a", "user-a", "run-v2")

    assert await manager.increment_retry("task-a") == 1
    assert await manager.increment_retry("task-a") == 2


@pytest.mark.asyncio
async def test_shared_manager_runs_two_users_without_rejection():
    from agent.pipeline_v2 import PipelineManagerV2

    db = MemoryDatabase()
    dependency = MagicMock()
    dependency.load = AsyncMock()
    manager = PipelineManagerV2({}, dependency, dependency, dependency, dependency, db)

    class SuccessfulGraph:
        async def ainvoke(self, state: dict):
            await asyncio.sleep(0.01)
            return dict(state)

    manager._graph = SuccessfulGraph()
    first, second = await asyncio.gather(
        manager.run_full(user_id="user-a", task_id="task-a"),
        manager.run_full(user_id="user-b", task_id="task-b"),
    )

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert db.tasks.documents["task-a"]["user_id"] == "user-a"
    assert db.tasks.documents["task-b"]["user_id"] == "user-b"
    assert db.tasks.documents["task-a"]["status"] == "completed"
    assert db.tasks.documents["task-b"]["status"] == "completed"
