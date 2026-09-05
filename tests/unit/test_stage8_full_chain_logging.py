"""阶段八任务 8.2：全链路 trace、skip 与 ERROR 日志测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agent.pipeline_v2 import classify_v2_node, create_state_v2
from api.pipeline import _run_v2_single_workflow, score_v2_single

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


class EmptyCursor:
    async def to_list(self, length=None):
        return []


class OneItemCursor:
    def __init__(self, item: dict) -> None:
        self.item = item

    async def to_list(self, length=None):
        return [self.item]


def _log_collection() -> MagicMock:
    collection = MagicMock()
    collection.insert_one = AsyncMock()
    return collection


@pytest.mark.asyncio
async def test_single_workflow_uses_one_trace_and_records_idempotent_skips() -> None:
    article = {
        "url_hash": ARTICLE_HASH,
        "title": "Existing article",
        "category_v2": "行业动态",
        "category_v2_confidence": 0.9,
        "is_ai_agent_security_relevant": True,
        "is_pr_eligible": False,
    }
    articles = MagicMock()
    articles.find_one = AsyncMock(return_value=article)
    pipeline_logs = _log_collection()
    db = {"articles": articles, "pipeline_logs": pipeline_logs}
    app = SimpleNamespace(state=SimpleNamespace(db=db, classifier_v2=MagicMock()))

    result = await _run_v2_single_workflow(
        app,
        ARTICLE_HASH,
        "user-a",
        task_id=None,
        trace_id="trace-shared",
        username="alice",
    )

    documents = [call.args[0] for call in pipeline_logs.insert_one.await_args_list]
    assert result["trace_id"] == "trace-shared"
    assert {document["trace_id"] for document in documents} == {"trace-shared"}
    assert {document["username"] for document in documents} == {"alice"}
    assert {(document["phase"], document["action"]) for document in documents} >= {
        ("classify_v2", "start"),
        ("classify_v2", "skip"),
        ("score_v2", "skip"),
        ("draft", "skip"),
    }


@pytest.mark.asyncio
async def test_stage_error_log_contains_type_message_and_stack_trace() -> None:
    article = {"_id": "article-1", "url_hash": ARTICLE_HASH}
    articles = MagicMock()
    articles.find.return_value = OneItemCursor(article)
    pipeline_logs = _log_collection()
    db = {"articles": articles, "pipeline_logs": pipeline_logs}
    classifier = MagicMock()
    classifier.classify_batch = AsyncMock(side_effect=RuntimeError("classifier unavailable"))
    state = create_state_v2(
        user_id="user-a",
        trace_id="trace-error",
        username="alice",
    )

    await classify_v2_node(state, classifier, db)

    documents = [call.args[0] for call in pipeline_logs.insert_one.await_args_list]
    error_document = next(document for document in documents if document["level"] == "ERROR")
    assert error_document["action"] == "error"
    assert error_document["trace_id"] == "trace-error"
    assert error_document["error"]["type"] == "RuntimeError"
    assert error_document["error"]["message"] == "classifier unavailable"
    assert "RuntimeError: classifier unavailable" in error_document["error"]["stack_trace"]


def test_state_contains_trace_and_username() -> None:
    state = create_state_v2(user_id="user-a", trace_id="trace-1", username="alice")
    assert state["trace_id"] == "trace-1"
    assert state["username"] == "alice"


@pytest.mark.asyncio
async def test_draft_style_logging_uses_profile_flag() -> None:
    article = {
        "url_hash": ARTICLE_HASH,
        "title": "Candidate",
        "pr_total_score": 100,
    }
    articles = MagicMock()
    articles.find.return_value = OneItemCursor(article)
    user_drafts = MagicMock()
    user_drafts.update_one = AsyncMock()
    pipeline_logs = _log_collection()
    db = {
        "articles": articles,
        "user_drafts": user_drafts,
        "pipeline_logs": pipeline_logs,
    }
    draft_gen = MagicMock()
    draft_gen.generate = AsyncMock(
        return_value={"ok": True, "drafts": [{"template": "新闻稿", "content": "draft"}]}
    )
    knowledge = MagicMock()
    knowledge.load = AsyncMock()
    state = create_state_v2(user_id="user-a", trace_id="trace-draft", username="alice")

    with patch("agent.pipeline_v2._load_style_hints", new=AsyncMock(return_value="formal")):
        from agent.pipeline_v2 import draft_node

        await draft_node(state, draft_gen, knowledge, db)

    documents = [call.args[0] for call in pipeline_logs.insert_one.await_args_list]
    completed = next(
        document
        for document in documents
        if document["phase"] == "draft" and document["action"] == "complete"
    )
    assert completed["detail"]["draft_count"] == 1
    assert completed["detail"]["style_hints_used"] is True
    assert completed["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_standalone_score_clears_old_score_and_stores_user_result() -> None:
    article = {
        "url_hash": ARTICLE_HASH,
        "pr_total_score": 95,
        "product_relevance": 48,
        "event_impact": 47,
    }
    articles = MagicMock()
    articles.find_one = AsyncMock(return_value=article)
    articles.update_one = AsyncMock()
    locks = MagicMock()
    locks.update_one = AsyncMock()
    locks.delete_one = AsyncMock()
    locks.insert_one = AsyncMock()
    user_scores = MagicMock()
    user_scores.update_one = AsyncMock()
    db = {
        "articles": articles,
        "pipeline_locks": locks,
        "user_article_scores": user_scores,
    }
    scorer = MagicMock()
    scorer.score_single = AsyncMock(
        return_value={
            "product_relevance": 60,
            "event_impact": 40,
            "pr_total_score": 100,
            "score_reason": "重新打分",
            "product_scores": [
                {"product_id": "p1", "product_name": "P1", "score": 60, "reason": ""}
            ],
            "is_pr_candidate": True,
        }
    )
    request = SimpleNamespace(
        state=SimpleNamespace(username="alice"),
        app=SimpleNamespace(state=SimpleNamespace(db=db, scorer_v2=scorer)),
    )

    response = await score_v2_single(ARTICLE_HASH, request, user_id="user-a")

    # 旧分数被清除（单篇打分按钮强制重新打分）
    clear_call = articles.update_one.await_args
    assert clear_call.args[1]["$set"]["pr_total_score"] is None
    # 结果写入用户级评分集合（与用户绑定）
    assert user_scores.update_one.await_args.args[0] == {
        "user_id": "user-a",
        "url_hash": ARTICLE_HASH,
    }
    assert response["skipped"] is False
    assert response["pr_total_score"] == 100
    assert response["is_pr_candidate"] is True
