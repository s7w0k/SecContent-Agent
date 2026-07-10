"""任务 7.5 流水线并发、幂等与用户草稿隔离测试。"""

from __future__ import annotations

import asyncio
import os
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import DuplicateKeyError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

import api.pipeline as pipeline_api
from api.pipeline import (
    PipelinePhaseRequest,
    acquire_pipeline_lock,
    classify_v2_single,
    pipeline_crawl,
    release_pipeline_lock,
    run_v2_single,
    score_v2_single,
    wait_for_pipeline_lock,
)

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


class FakeLockCollection:
    def __init__(self):
        self.documents: dict[str, dict] = {}

    async def insert_one(self, document: dict):
        key = document["lock_key"]
        if key in self.documents:
            raise DuplicateKeyError("duplicate lock")
        self.documents[key] = deepcopy(document)
        return SimpleNamespace(inserted_id=key)

    async def find_one(self, query: dict):
        document = self.documents.get(query["lock_key"])
        return deepcopy(document) if document else None

    async def update_one(self, query: dict, update: dict):
        document = self.documents.get(query["lock_key"])
        if not document:
            return SimpleNamespace(modified_count=0)
        document.update(deepcopy(update.get("$set", {})))
        return SimpleNamespace(modified_count=1)

    async def delete_one(self, query: dict):
        key = query["lock_key"]
        document = self.documents.get(key)
        if not document:
            return SimpleNamespace(deleted_count=0)
        expires_query = query.get("expires_at")
        if expires_query:
            document_expires = document["expires_at"]
            if document_expires.tzinfo is None:
                document_expires = document_expires.replace(tzinfo=UTC)
            if document_expires > expires_query["$lte"]:
                return SimpleNamespace(deleted_count=0)
        del self.documents[key]
        return SimpleNamespace(deleted_count=1)


class FakeArticleCollection:
    def __init__(self, document: dict):
        self.document = deepcopy(document)
        self.update_calls: list[tuple[dict, dict]] = []

    async def find_one(self, query: dict):
        if query.get("url_hash") != self.document.get("url_hash"):
            return None
        return deepcopy(self.document)

    async def update_one(self, query: dict, update: dict, **_kwargs):
        self.update_calls.append((deepcopy(query), deepcopy(update)))
        self.document.update(deepcopy(update.get("$set", {})))
        return SimpleNamespace(modified_count=1)


class FakeCaptureCollection:
    def __init__(self, find_result: dict | None = None):
        self.find_result = find_result
        self.update_calls: list[tuple[dict, dict, dict]] = []

    async def find_one(self, _query: dict):
        return deepcopy(self.find_result)

    async def update_one(self, query: dict, update: dict, **kwargs):
        self.update_calls.append((deepcopy(query), deepcopy(update), deepcopy(kwargs)))
        return SimpleNamespace(modified_count=1)


class FakeDatabase:
    def __init__(self, **collections):
        self.collections = collections

    def __getitem__(self, name: str):
        return self.collections[name]


def _request(db, **state_values):
    state = SimpleNamespace(db=db, **state_values)
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.fixture
def fast_lock_poll(monkeypatch):
    original_sleep = asyncio.sleep

    async def _fast_sleep(_seconds: float):
        await original_sleep(0)

    monkeypatch.setattr(pipeline_api.asyncio, "sleep", _fast_sleep)


@pytest.mark.asyncio
async def test_lock_helpers_enforce_unique_key_and_five_minute_ttl():
    locks = FakeLockCollection()
    db = FakeDatabase(pipeline_locks=locks)
    before = datetime.now(UTC)

    assert await acquire_pipeline_lock(db, "crawl-today", "user-a") is True
    assert await acquire_pipeline_lock(db, "crawl-today", "user-b") is False
    document = locks.documents["crawl-today"]
    assert document["user_id"] == "user-a"
    assert timedelta(seconds=299) <= document["expires_at"] - before <= timedelta(seconds=301)

    await release_pipeline_lock(db, "crawl-today", success=True)
    assert (
        await wait_for_pipeline_lock(db, "crawl-today", timeout=1, poll_interval=0) == "completed"
    )
    await release_pipeline_lock(db, "crawl-today", success=False)
    assert await wait_for_pipeline_lock(db, "crawl-today", timeout=1, poll_interval=0) == "failed"


@pytest.mark.asyncio
async def test_expired_lock_is_removed_without_waiting_for_ttl_monitor():
    locks = FakeLockCollection()
    locks.documents["expired"] = {
        "lock_key": "expired",
        "status": "running",
        "expires_at": datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
    }
    db = FakeDatabase(pipeline_locks=locks)

    assert await wait_for_pipeline_lock(db, "expired", timeout=1, poll_interval=0) == "failed"
    assert "expired" not in locks.documents


@pytest.mark.asyncio
async def test_concurrent_crawl_runs_once_and_second_request_reuses(
    fast_lock_poll,
):
    locks = FakeLockCollection()
    db = FakeDatabase(pipeline_locks=locks)
    started = asyncio.Event()
    finish = asyncio.Event()
    manager = MagicMock()

    async def _run_phase(*_args, **_kwargs):
        started.set()
        await finish.wait()
        return {"ok": True, "pipeline_id": "pipeline-1"}

    manager.run_phase = AsyncMock(side_effect=_run_phase)
    request = _request(db, pipeline_manager=manager)

    first = asyncio.create_task(
        pipeline_crawl(PipelinePhaseRequest(crawl_days=1), request, user_id="user-a"),
    )
    await started.wait()
    second = asyncio.create_task(
        pipeline_crawl(PipelinePhaseRequest(crawl_days=1), request, user_id="user-b"),
    )
    await asyncio.sleep(0)
    finish.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert manager.run_phase.await_count == 1
    assert first_result["pipeline_id"] == "pipeline-1"
    assert second_result["skipped"] is True


@pytest.mark.asyncio
async def test_concurrent_classification_calls_llm_once(fast_lock_poll):
    locks = FakeLockCollection()
    articles = FakeArticleCollection(
        {"url_hash": ARTICLE_HASH, "title": "MCP event", "category_v2": None},
    )
    db = FakeDatabase(pipeline_locks=locks, articles=articles)
    started = asyncio.Event()
    finish = asyncio.Event()
    classifier = MagicMock()

    async def _classify(_article):
        started.set()
        await finish.wait()
        return SimpleNamespace(
            category="热点事件",
            confidence=0.96,
            reason="relevant",
            is_fallback=False,
            is_pr_eligible=True,
        )

    classifier.classify_single = AsyncMock(side_effect=_classify)
    request = _request(db, classifier_v2=classifier)

    first = asyncio.create_task(classify_v2_single(ARTICLE_HASH, request, user_id="user-a"))
    await started.wait()
    second = asyncio.create_task(classify_v2_single(ARTICLE_HASH, request, user_id="user-b"))
    await asyncio.sleep(0)
    finish.set()
    results = await asyncio.gather(first, second)

    assert classifier.classify_single.await_count == 1
    assert {result["skipped"] for result in results} == {False, True}
    assert articles.document["category_v2"] == "热点事件"


@pytest.mark.asyncio
async def test_concurrent_scoring_calls_llm_once(fast_lock_poll):
    locks = FakeLockCollection()
    articles = FakeArticleCollection(
        {"url_hash": ARTICLE_HASH, "title": "MCP event", "pr_total_score": None},
    )
    db = FakeDatabase(pipeline_locks=locks, articles=articles)
    started = asyncio.Event()
    finish = asyncio.Event()
    scorer = MagicMock()

    async def _score(_article):
        started.set()
        await finish.wait()
        return {
            "product_relevance": 90,
            "event_impact": 80,
            "pr_total_score": 170,
            "score_reason": "candidate",
            "is_pr_candidate": True,
        }

    scorer.score_single = AsyncMock(side_effect=_score)
    request = _request(db, scorer_v2=scorer)

    first = asyncio.create_task(score_v2_single(ARTICLE_HASH, request, user_id="user-a"))
    await started.wait()
    second = asyncio.create_task(score_v2_single(ARTICLE_HASH, request, user_id="user-b"))
    await asyncio.sleep(0)
    finish.set()
    results = await asyncio.gather(first, second)

    assert scorer.score_single.await_count == 1
    assert {result["skipped"] for result in results} == {False, True}
    assert articles.document["pr_total_score"] == 170


@pytest.mark.asyncio
async def test_concurrent_drafts_are_upserted_per_user():
    articles = FakeArticleCollection(
        {"url_hash": ARTICLE_HASH, "title": "MCP event", "content_md": "content"},
    )
    user_profiles = FakeCaptureCollection(find_result=None)
    user_drafts = FakeCaptureCollection()
    db = FakeDatabase(
        articles=articles,
        user_profiles=user_profiles,
        user_drafts=user_drafts,
    )
    classifier = MagicMock()
    classifier.classify_single = AsyncMock(
        return_value=SimpleNamespace(
            category="热点事件",
            confidence=0.96,
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
    request = _request(
        db,
        classifier_v2=classifier,
        scorer_v2=scorer,
        draft_gen=draft_gen,
    )

    await asyncio.gather(
        run_v2_single(ARTICLE_HASH, request, user_id="user-a"),
        run_v2_single(ARTICLE_HASH, request, user_id="user-b"),
    )

    queries = [call[0] for call in user_drafts.update_calls]
    assert {query["user_id"] for query in queries} == {"user-a", "user-b"}
    assert {query["article_url_hash"] for query in queries} == {ARTICLE_HASH}
    assert all(call[2] == {"upsert": True} for call in user_drafts.update_calls)
