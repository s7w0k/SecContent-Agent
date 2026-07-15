"""任务 7.6 异步流水线任务测试。"""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from api.pipeline import (
    PipelineRunRequest,
    _create_pipeline_task,
    _execute_pipeline_task,
    _read_task_checkpoints,
    get_pipeline_task,
    list_pipeline_tasks,
    list_task_checkpoints,
    pipeline_run_v2,
    pipeline_status_v2,
    resume_pipeline_task,
)

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


class FakeCursor:
    def __init__(self, documents: list[dict]):
        self.documents = documents

    def sort(self, _field: str, _direction: int):
        self.documents.sort(key=lambda item: item["created_at"], reverse=True)
        return self

    def skip(self, count: int):
        self.documents = self.documents[count:]
        return self

    def limit(self, count: int):
        self.documents = self.documents[:count]
        return self

    async def to_list(self, length: int):
        return deepcopy(self.documents[:length])


class FakeTaskCollection:
    def __init__(self):
        self.documents: dict[str, dict] = {}
        self.history: list[dict] = []

    async def insert_one(self, document: dict):
        self.documents[document["task_id"]] = deepcopy(document)
        return SimpleNamespace(inserted_id=document["task_id"])

    async def update_one(self, query: dict, update: dict):
        document = self.documents.get(query["task_id"])
        if document and ("user_id" not in query or document.get("user_id") == query.get("user_id")):
            fields = deepcopy(update["$set"])
            document.update(fields)
            self.history.append(fields)
            return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def find_one(self, query: dict):
        document = self.documents.get(query["task_id"])
        return deepcopy(document) if document else None

    async def count_documents(self, query: dict):
        return sum(item.get("user_id") == query.get("user_id") for item in self.documents.values())

    def find(self, query: dict):
        return FakeCursor(
            [
                deepcopy(item)
                for item in self.documents.values()
                if item.get("user_id") == query.get("user_id")
            ],
        )


class FakeCollection:
    def __init__(self, document: dict | None = None):
        self.document = deepcopy(document)
        self.update_calls: list[tuple[dict, dict, dict]] = []

    async def find_one(self, query: dict):
        if self.document and (
            "url_hash" not in query or query["url_hash"] == self.document.get("url_hash")
        ):
            return deepcopy(self.document)
        return None

    async def update_one(self, query: dict, update: dict, **kwargs):
        self.update_calls.append((deepcopy(query), deepcopy(update), deepcopy(kwargs)))
        if self.document is not None:
            self.document.update(deepcopy(update.get("$set", {})))
        return SimpleNamespace(modified_count=1)

    async def insert_one(self, _document: dict):
        return SimpleNamespace(inserted_id="id")


class FakeDatabase:
    def __init__(self, **collections):
        self.collections = collections

    def __getitem__(self, name: str):
        return self.collections[name]


def _app(db, **values):
    return SimpleNamespace(state=SimpleNamespace(db=db, **values))


def _single_pipeline_app(*, classifier_error: Exception | None = None):
    tasks = FakeTaskCollection()
    articles = FakeCollection(
        {"url_hash": ARTICLE_HASH, "title": "MCP event", "content_md": "content"},
    )
    profiles = FakeCollection(None)
    drafts = FakeCollection(None)
    activities = FakeCollection(None)
    db = FakeDatabase(
        pipeline_tasks=tasks,
        articles=articles,
        user_profiles=profiles,
        user_drafts=drafts,
        user_activities=activities,
    )
    classifier = MagicMock()
    if classifier_error:
        classifier.classify_single = AsyncMock(side_effect=classifier_error)
    else:
        classifier.classify_single = AsyncMock(
            return_value=SimpleNamespace(
                category="热点事件",
                confidence=0.95,
                reason="relevant",
                is_fallback=False,
                is_pr_eligible=True,
            ),
        )
    scorer = MagicMock()
    scorer.score_single = AsyncMock(
        return_value={
            "product_relevance": 90,
            "event_impact": 80,
            "pr_total_score": 170,
            "score_reason": "candidate",
            "is_pr_candidate": True,
        },
    )
    draft_gen = MagicMock()
    draft_gen.generate = AsyncMock(
        return_value={
            "ok": True,
            "drafts": [{"template": "爆点A", "content_md": "# draft"}],
        },
    )
    return (
        _app(
            db,
            classifier_v2=classifier,
            scorer_v2=scorer,
            draft_gen=draft_gen,
        ),
        tasks,
        drafts,
    )


@pytest.mark.asyncio
async def test_single_pipeline_task_persists_progress_and_result():
    app, tasks, drafts = _single_pipeline_app()
    document = await _create_pipeline_task(
        app.state.db,
        "user-a",
        "run-v2",
        ARTICLE_HASH,
    )

    await _execute_pipeline_task(
        app,
        document["task_id"],
        "user-a",
        "run-v2",
        article_url_hash=ARTICLE_HASH,
    )

    stored = tasks.documents[document["task_id"]]
    assert stored["status"] == "completed"
    assert stored["progress"]["phase"] == "completed"
    assert stored["result"]["ok"] is True
    phases = [item["progress"]["phase"] for item in tasks.history if "progress" in item]
    assert phases == ["classify", "classify", "score", "draft", "completed"]
    assert drafts.update_calls[0][0] == {
        "user_id": "user-a",
        "article_url_hash": ARTICLE_HASH,
    }
    assert drafts.update_calls[0][2] == {"upsert": True}


@pytest.mark.asyncio
async def test_pipeline_task_failure_is_persisted():
    app, tasks, _drafts = _single_pipeline_app(classifier_error=RuntimeError("LLM unavailable"))
    document = await _create_pipeline_task(
        app.state.db,
        "user-a",
        "run-v2",
        ARTICLE_HASH,
    )

    await _execute_pipeline_task(
        app,
        document["task_id"],
        "user-a",
        "run-v2",
        article_url_hash=ARTICLE_HASH,
    )

    stored = tasks.documents[document["task_id"]]
    assert stored["status"] == "failed"
    assert stored["progress"]["phase"] == "failed"
    assert stored["error"] == "LLM unavailable"


@pytest.mark.asyncio
async def test_run_v2_endpoint_enqueues_worker_job():
    tasks = FakeTaskCollection()
    activities = FakeCollection(None)
    db = FakeDatabase(pipeline_tasks=tasks, user_activities=activities)
    arq_pool = MagicMock()
    arq_pool.enqueue_job = AsyncMock(return_value=SimpleNamespace(job_id="queued"))
    app = _app(db, arq_pool=arq_pool)
    request = SimpleNamespace(app=app)

    response = await pipeline_run_v2(
        PipelineRunRequest(crawl_days=1),
        request,
        user_id="user-a",
    )
    task_id = response["data"]["task_id"]
    assert tasks.documents[task_id]["status"] == "pending"
    arq_pool.enqueue_job.assert_awaited_once_with(
        "execute_pipeline",
        task_id=task_id,
        user_id="user-a",
        task_type="run-v2",
        crawl_days=1,
        article_url_hash=None,
        trace_id=response["data"]["trace_id"],
        username="user-a",
        request_id="",
        _job_id=task_id,
    )


@pytest.mark.asyncio
async def test_task_queries_enforce_user_isolation_and_list_filtering():
    tasks = FakeTaskCollection()
    db = FakeDatabase(pipeline_tasks=tasks)
    user_a = await _create_pipeline_task(db, "user-a", "crawl")
    await _create_pipeline_task(db, "user-b", "crawl")
    request = SimpleNamespace(app=_app(db))

    own = await get_pipeline_task(user_a["task_id"], request, user_id="user-a")
    assert own["data"]["user_id"] == "user-a"

    with pytest.raises(HTTPException) as exc_info:
        await get_pipeline_task(user_a["task_id"], request, user_id="user-b")
    assert exc_info.value.status_code == 403

    result = await list_pipeline_tasks(request, page=1, page_size=20, user_id="user-a")
    assert result["data"]["total"] == 1
    assert {item["user_id"] for item in result["data"]["items"]} == {"user-a"}


@pytest.mark.asyncio
async def test_status_v2_returns_persisted_task_and_rejects_other_user():
    tasks = FakeTaskCollection()
    db = FakeDatabase(pipeline_tasks=tasks)
    task = await _create_pipeline_task(db, "user-a", "run-v2")
    tasks.documents[task["task_id"]].update(
        {
            "status": "running",
            "last_node": "classify_v2",
            "retry_count": 1,
            "progress": {"phase": "score_v2", "current": 3, "total": 8},
        }
    )
    request = SimpleNamespace(app=_app(db))

    result = await pipeline_status_v2(request, task_id=task["task_id"], user_id="user-a")

    assert result["status"] == "running"
    assert result["current_phase"] == "score_v2"
    assert result["last_node"] == "classify_v2"
    assert result["retry_count"] == 1
    with pytest.raises(HTTPException) as exc_info:
        await pipeline_status_v2(request, task_id=task["task_id"], user_id="user-b")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_checkpoint_endpoint_is_tenant_isolated():
    tasks = FakeTaskCollection()
    db = FakeDatabase(pipeline_tasks=tasks)
    task = await _create_pipeline_task(db, "user-a", "run-v2")
    request = SimpleNamespace(app=_app(db))
    checkpoint = {"checkpoint_id": "ckpt-1", "node": "score_v2"}

    with patch("api.pipeline._read_task_checkpoints", new=AsyncMock(return_value=[checkpoint])):
        result = await list_task_checkpoints(task["task_id"], request, limit=100, user_id="user-a")
        with pytest.raises(HTTPException) as exc_info:
            await list_task_checkpoints(task["task_id"], request, limit=100, user_id="user-b")

    assert result["data"]["checkpoints"] == [checkpoint]
    assert result["data"]["thread_id"] == f"thread-{task['task_id']}"
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_checkpoint_reader_decodes_langgraph_metadata():
    class FakeSaver:
        async def alist(self, config, *, limit):
            assert config["configurable"]["thread_id"] == "thread-task-a"
            assert limit == 10
            yield SimpleNamespace(
                config={"configurable": {"checkpoint_id": "ckpt-2"}},
                parent_config={"configurable": {"checkpoint_id": "ckpt-1"}},
                checkpoint={
                    "ts": "2026-07-14T10:00:00Z",
                    "channel_values": {"current_phase": "score_v2", "scored_v2_count": 2},
                },
                metadata={"step": 3, "writes": {"score_v2": {}}},
            )

    task = {"task_id": "task-a", "thread_id": "thread-task-a", "checkpoint_ns": ""}
    with (
        patch("api.pipeline.supports_mongodb_checkpoints", return_value=True),
        patch("api.pipeline.create_checkpointer", return_value=FakeSaver()),
    ):
        checkpoints = await _read_task_checkpoints(MagicMock(), task, limit=10)

    assert checkpoints == [
        {
            "checkpoint_id": "ckpt-2",
            "parent_checkpoint_id": "ckpt-1",
            "node": "score_v2",
            "step": 3,
            "created_at": "2026-07-14T10:00:00Z",
            "channel_values": {"current_phase": "score_v2", "scored_v2_count": 2},
        }
    ]


@pytest.mark.asyncio
async def test_resume_endpoint_enqueues_worker_and_rejects_completed_task():
    tasks = FakeTaskCollection()
    db = FakeDatabase(pipeline_tasks=tasks)
    task = await _create_pipeline_task(db, "user-a", "run-v2")
    tasks.documents[task["task_id"]]["status"] = "failed"
    arq_pool = MagicMock()
    arq_pool.enqueue_job = AsyncMock(return_value=SimpleNamespace(job_id="resume-job"))
    request = SimpleNamespace(app=_app(db, arq_pool=arq_pool))
    checkpoint = {"checkpoint_id": "ckpt-1", "node": "score_v2"}

    with patch("api.pipeline._read_task_checkpoints", new=AsyncMock(return_value=[checkpoint])):
        result = await resume_pipeline_task(task["task_id"], request, user_id="user-a")

    assert result["data"]["resumed_from"] == "score_v2"
    assert tasks.documents[task["task_id"]]["status"] == "pending"
    assert tasks.documents[task["task_id"]]["retry_count"] == 0
    enqueue = arq_pool.enqueue_job.await_args
    assert enqueue.args == ("resume_pipeline",)
    assert enqueue.kwargs["task_id"] == task["task_id"]
    assert enqueue.kwargs["user_id"] == "user-a"
    assert enqueue.kwargs["_job_id"].startswith(f"resume-{task['task_id']}-")

    tasks.documents[task["task_id"]]["status"] = "completed"
    with pytest.raises(HTTPException) as exc_info:
        await resume_pipeline_task(task["task_id"], request, user_id="user-a")
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "TASK_NOT_RESUMABLE"

    tasks.documents[task["task_id"]]["status"] = "failed"
    with (
        patch("api.pipeline._read_task_checkpoints", new=AsyncMock(return_value=[])),
        pytest.raises(HTTPException) as no_checkpoint,
    ):
        await resume_pipeline_task(task["task_id"], request, user_id="user-a")
    assert no_checkpoint.value.status_code == 404
    assert no_checkpoint.value.detail["code"] == "CHECKPOINT_NOT_FOUND"
