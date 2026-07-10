"""任务 7.6 异步流水线任务测试。"""

from __future__ import annotations

import asyncio
import os
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from api.pipeline import (
    PipelineRunRequest,
    _create_pipeline_task,
    _execute_pipeline_task,
    get_pipeline_task,
    list_pipeline_tasks,
    pipeline_run_v2,
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
        if document and document.get("user_id") == query.get("user_id"):
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
async def test_run_v2_endpoint_returns_before_background_finishes():
    tasks = FakeTaskCollection()
    activities = FakeCollection(None)
    db = FakeDatabase(pipeline_tasks=tasks, user_activities=activities)
    started = asyncio.Event()
    finish = asyncio.Event()
    manager = MagicMock()
    manager.get_status = MagicMock(
        return_value={"current_phase": "crawl", "status": "running"},
    )

    async def _run_full(**_kwargs):
        started.set()
        await finish.wait()
        return {"pipeline_id": "p-1", "status": "completed", "state": {}}

    manager.run_full = AsyncMock(side_effect=_run_full)
    app = _app(db, pipeline_v2=manager)
    request = SimpleNamespace(app=app)

    response = await pipeline_run_v2(
        PipelineRunRequest(crawl_days=1),
        request,
        user_id="user-a",
    )
    await started.wait()

    task_id = response["data"]["task_id"]
    assert tasks.documents[task_id]["status"] == "running"
    background = list(app.state.pipeline_background_tasks)
    assert background and not background[0].done()

    finish.set()
    await asyncio.gather(*background)
    assert tasks.documents[task_id]["status"] == "completed"


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
