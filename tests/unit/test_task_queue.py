"""ARQ task queue behavior tests."""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from arq.worker import Retry

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


class TaskCollection:
    def __init__(self, document: dict | None = None):
        self.document = deepcopy(document)

    async def find_one(self, query: dict):
        if self.document and all(self.document.get(key) == value for key, value in query.items()):
            return deepcopy(self.document)
        return None

    async def update_one(self, query: dict, update: dict):
        if self.document and self.document.get("task_id") == query.get("task_id"):
            self.document.update(deepcopy(update.get("$set", {})))
        return SimpleNamespace(modified_count=int(self.document is not None))

    async def find_one_and_update(self, query: dict, update: dict, **_kwargs):
        if not self.document or self.document.get("task_id") != query.get("task_id"):
            return None
        for key, value in update.get("$inc", {}).items():
            self.document[key] = self.document.get(key, 0) + value
        self.document.update(deepcopy(update.get("$set", {})))
        return deepcopy(self.document)


class Database:
    def __init__(self, document: dict | None):
        self.tasks = TaskCollection(document)

    def __getitem__(self, name: str):
        assert name == "pipeline_tasks"
        return self.tasks


def _task(retry_count: int = 0) -> dict:
    return {
        "task_id": "task-a",
        "user_id": "user-a",
        "status": "pending",
        "retry_count": retry_count,
    }


@pytest.mark.asyncio
async def test_execute_pipeline_runs_worker_operation():
    from agent.task_queue import execute_pipeline

    db = Database(_task())

    async def complete(_app, task_id, user_id, task_type, **kwargs):
        assert (task_id, user_id, task_type) == ("task-a", "user-a", "run-v2")
        assert kwargs["raise_errors"] is True
        db.tasks.document["status"] = "completed"

    with patch("api.pipeline._execute_pipeline_task", new=AsyncMock(side_effect=complete)):
        result = await execute_pipeline(
            {"db": db, "app": SimpleNamespace()},
            "task-a",
            "user-a",
            "run-v2",
        )

    assert result == {"task_id": "task-a", "status": "completed"}


@pytest.mark.asyncio
async def test_pipeline_task_is_enqueued_with_durable_job_id():
    from api.pipeline import _enqueue_pipeline_task

    pool = SimpleNamespace(
        enqueue_job=AsyncMock(return_value=SimpleNamespace(job_id="task-a")),
    )
    app = SimpleNamespace(state=SimpleNamespace(arq_pool=pool, db=None))

    await _enqueue_pipeline_task(
        app,
        "task-a",
        "user-a",
        "run-v2",
        crawl_days=3,
        trace_id="trace-a",
        username="alice",
        request_id="request-a",
    )

    pool.enqueue_job.assert_awaited_once_with(
        "execute_pipeline",
        task_id="task-a",
        user_id="user-a",
        task_type="run-v2",
        crawl_days=3,
        article_url_hash=None,
        trace_id="trace-a",
        username="alice",
        request_id="request-a",
        _job_id="task-a",
    )


@pytest.mark.asyncio
async def test_execute_pipeline_returns_failed_for_missing_task():
    from agent.task_queue import execute_pipeline

    result = await execute_pipeline(
        {"db": Database(None), "app": SimpleNamespace()},
        "missing",
        "user-a",
        "run-v2",
    )

    assert result == {"status": "failed", "error": "task not found"}


@pytest.mark.asyncio
async def test_execute_pipeline_requests_retry_after_failure():
    from agent.task_queue import execute_pipeline

    db = Database(_task())
    with (
        patch(
            "api.pipeline._execute_pipeline_task",
            new=AsyncMock(side_effect=RuntimeError("temporary")),
        ),
        pytest.raises(Retry),
    ):
        await execute_pipeline(
            {"db": db, "app": SimpleNamespace()},
            "task-a",
            "user-a",
            "run-v2",
        )

    assert db.tasks.document["retry_count"] == 1


@pytest.mark.asyncio
async def test_execute_pipeline_stops_after_retry_limit():
    from agent.task_queue import execute_pipeline
    from config import get_settings

    db = Database(_task(retry_count=get_settings().ARQ_MAX_RETRIES))
    with (
        patch(
            "api.pipeline._execute_pipeline_task",
            new=AsyncMock(side_effect=RuntimeError("permanent")),
        ),
        pytest.raises(RuntimeError, match="permanent"),
    ):
        await execute_pipeline(
            {"db": db, "app": SimpleNamespace()},
            "task-a",
            "user-a",
            "run-v2",
        )


def test_worker_settings_follow_application_config():
    from agent.task_queue import WorkerSettings
    from config import get_settings

    settings = get_settings()
    assert WorkerSettings.max_jobs == settings.ARQ_MAX_JOBS
    assert WorkerSettings.job_timeout == settings.ARQ_JOB_TIMEOUT
    assert WorkerSettings.max_tries == settings.ARQ_MAX_RETRIES + 1
    assert WorkerSettings.health_check_interval == 15
    assert {function.name for function in WorkerSettings.functions} == {
        "execute_pipeline",
        "fetch_fulltext_batch",
        "resume_pipeline",
    }


def test_worker_timeout_is_applied_to_all_queue_jobs():
    from agent.task_queue import WorkerSettings
    from config import get_settings

    assert WorkerSettings.job_timeout == get_settings().ARQ_JOB_TIMEOUT
    assert WorkerSettings.job_timeout > 0
    assert all(function.timeout_s is None for function in WorkerSettings.functions)


@pytest.mark.asyncio
async def test_fetch_fulltext_batch_delegates_to_background_service():
    from agent.task_queue import fetch_fulltext_batch

    db = SimpleNamespace()
    articles = [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}]
    background_fetch = AsyncMock()
    crawl_client = SimpleNamespace()

    with patch("agent.pipeline._fetch_fulltext_background", new=background_fetch):
        result = await fetch_fulltext_batch(
            {"db": db, "mcp_crawl_client": crawl_client},
            articles,
            trace_id="trace-a",
            user_id="user-a",
            request_id="request-a",
        )

    background_fetch.assert_awaited_once_with(
        db,
        articles,
        "trace-a",
        client=crawl_client,
        user_id="user-a",
        request_id="request-a",
    )
    assert result == {"requested": 2}


@pytest.mark.asyncio
async def test_resume_pipeline_returns_failed_for_missing_task():
    from agent.task_queue import resume_pipeline

    pipeline_v2 = SimpleNamespace(resume_from_checkpoint=AsyncMock())
    result = await resume_pipeline(
        {"db": Database(None), "pipeline_v2": pipeline_v2},
        "missing",
        "user-a",
    )

    assert result == {"status": "failed", "error": "task not found"}
    pipeline_v2.resume_from_checkpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_pipeline_runs_checkpoint_recovery():
    from agent.task_queue import resume_pipeline

    db = Database(_task())
    pipeline_v2 = SimpleNamespace(
        resume_from_checkpoint=AsyncMock(
            return_value={"pipeline_id": "task-a", "status": "completed"}
        )
    )

    result = await resume_pipeline(
        {"db": db, "pipeline_v2": pipeline_v2},
        "task-a",
        "user-a",
    )

    pipeline_v2.resume_from_checkpoint.assert_awaited_once_with("task-a")
    assert result["status"] == "completed"
