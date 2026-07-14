"""Tests for LangGraph MongoDB checkpoint integration."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


def test_create_checkpointer_reuses_motor_delegate():
    from agent.checkpointer import create_checkpointer

    sync_client = MongoClient("mongodb://localhost:27017", connect=False)
    db = SimpleNamespace(
        client=SimpleNamespace(delegate=sync_client),
        name="pr_agent",
    )
    saver = MagicMock()
    try:
        with patch("agent.checkpointer.MongoDBSaver", return_value=saver) as saver_cls:
            result = create_checkpointer(db)
    finally:
        sync_client.close()

    assert result is saver
    saver_cls.assert_called_once_with(
        sync_client,
        db_name="pr_agent",
        checkpoint_collection_name="pipeline_checkpoints",
        writes_collection_name="pipeline_checkpoint_writes",
    )


def test_create_checkpointer_rejects_non_mongo_database():
    from agent.checkpointer import create_checkpointer

    with pytest.raises(TypeError, match="Motor or PyMongo"):
        create_checkpointer(MagicMock())


def _manager_without_db():
    from agent.pipeline_v2 import PipelineManagerV2

    dependency = MagicMock()
    dependency.load = AsyncMock()
    return PipelineManagerV2({}, dependency, dependency, dependency, dependency, None)


@pytest.mark.asyncio
async def test_run_full_uses_task_thread_id():
    manager = _manager_without_db()
    manager._graph = MagicMock()
    manager._graph.ainvoke = AsyncMock(
        side_effect=lambda state, config: dict(state),
    )

    result = await manager.run_full(user_id="user-a", task_id="task-a")

    assert result["status"] == "completed"
    config = manager._graph.ainvoke.await_args.kwargs["config"]
    assert config == {"configurable": {"thread_id": "thread-task-a"}}


@pytest.mark.asyncio
async def test_resume_from_latest_checkpoint():
    from agent.pipeline_v2 import create_state_v2

    manager = _manager_without_db()
    saved_state = create_state_v2(user_id="user-a")
    saved_state["current_phase"] = "score_v2"
    manager._graph = MagicMock()
    manager._graph.aget_state = AsyncMock(
        return_value=SimpleNamespace(values=saved_state, next=("draft",)),
    )
    manager._graph.ainvoke = AsyncMock(return_value=dict(saved_state))

    result = await manager.resume_from_checkpoint("task-a")

    config = {"configurable": {"thread_id": "thread-task-a"}}
    manager._graph.aget_state.assert_awaited_once_with(config)
    manager._graph.ainvoke.assert_awaited_once_with(None, config=config)
    assert result["status"] == "completed"
    assert result["state"]["current_phase"] == "score_v2"


@pytest.mark.asyncio
async def test_resume_returns_failed_without_checkpoint():
    manager = _manager_without_db()
    manager._graph = MagicMock()
    manager._graph.aget_state = AsyncMock(
        return_value=SimpleNamespace(values={}, next=()),
    )

    result = await manager.resume_from_checkpoint("missing-task")

    assert result == {
        "pipeline_id": "missing-task",
        "status": "failed",
        "error": "No checkpoint found",
    }
