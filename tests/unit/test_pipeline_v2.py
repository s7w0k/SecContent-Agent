"""
Agent 流水线编排 V2 — 单元测试

运行:
    pytest tests/unit/test_pipeline_v2.py -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def mock_tools():
    """Mock MCP tools"""
    tool = MagicMock()
    tool.ainvoke = AsyncMock(return_value={"ok": True, "data": {"articles": []}})
    return {
        "crawl_overseas_news": tool,
        "fetch_wewe_articles": MagicMock(),
        "classify_articles": MagicMock(),
        "query_articles": MagicMock(),
        "get_crawl_stats": MagicMock(),
        "export_articles_csv": MagicMock(),
        "fetch_article_fulltext": MagicMock(),
        "analyze_wewe_article": MagicMock(),
    }


@pytest.fixture
def mock_classifier():
    """Mock ClassifierV2"""
    from agent.classifier_v2 import ClassifyResultV2

    classifier = MagicMock()
    classifier.classify_batch = AsyncMock(
        return_value=[
            ClassifyResultV2(category="爆点事件", confidence=90, reason="重大漏洞"),
            ClassifyResultV2(category="国内外竞品信息", confidence=80, reason="友商动态"),
        ]
    )
    return classifier


@pytest.fixture
def mock_scorer():
    """Mock ScoringAgentV2"""
    scorer = MagicMock()
    scorer.adjust_threshold = AsyncMock(
        return_value={
            "base_threshold": 80,
            "adjustment": 0,
            "threshold": 80,
            "feedback_count": 0,
            "directional_count": 0,
        }
    )
    scorer.score_batch = AsyncMock(
        return_value=[
            {
                "product_relevance": 85,
                "event_impact": 72,
                "pr_total_score": 157,
                "is_pr_candidate": True,
                "_fallback": False,
            },
            {
                "product_relevance": 25,
                "event_impact": 30,
                "pr_total_score": 55,
                "is_pr_candidate": False,
                "_fallback": False,
            },
        ]
    )
    return scorer


@pytest.fixture
def mock_draft_gen():
    """Mock DraftGenerator"""
    gen = MagicMock()
    gen.generate = AsyncMock(
        return_value={
            "ok": True,
            "drafts": [
                {
                    "template": "爆点A",
                    "perspective": "角度1",
                    "content_md": "# Draft",
                    "title": "T",
                    "index": 1,
                },
            ],
            "error": None,
        }
    )
    return gen


@pytest.fixture
def mock_knowledge():
    k = MagicMock()
    k.load = AsyncMock()
    k.as_system_prompt = MagicMock(return_value="test knowledge")
    return k


@pytest.fixture
def mock_db():
    db = MagicMock()
    articles_mock = MagicMock()
    db.__getitem__ = MagicMock(return_value=articles_mock)

    mock_cursor = MagicMock()
    mock_cursor.to_list = AsyncMock(return_value=[])
    articles_mock.find = MagicMock(return_value=mock_cursor)
    articles_mock.find_one = AsyncMock(return_value=None)
    articles_mock.insert_one = AsyncMock()
    articles_mock.update_one = AsyncMock()
    articles_mock.count_documents = AsyncMock(return_value=0)

    return db


@pytest.fixture
def manager(mock_tools, mock_classifier, mock_scorer, mock_draft_gen, mock_knowledge, mock_db):
    from agent.pipeline_v2 import PipelineManagerV2

    return PipelineManagerV2(
        tools=mock_tools,
        classifier_v2=mock_classifier,
        scorer_v2=mock_scorer,
        draft_gen=mock_draft_gen,
        knowledge=mock_knowledge,
        db=mock_db,
    )


# ═══════════════════════════════════════════════════════════════
# 1. create_state_v2 测试
# ═══════════════════════════════════════════════════════════════


class TestCreateStateV2:
    def test_default_state(self):
        from agent.pipeline_v2 import create_state_v2

        state = create_state_v2()
        assert len(state["phases"]) == 9
        assert "crawl" in state["phases"]
        assert "enrich" in state["phases"]
        assert "classify_v2" in state["phases"]
        assert "filter" in state["phases"]
        assert "score_v2" in state["phases"]
        assert "draft" in state["phases"]
        assert "quality_check" in state["phases"]
        assert "rewrite" in state["phases"]
        assert "review" in state["phases"]
        assert state["crawl_days"] == 1


# ═══════════════════════════════════════════════════════════════
# 2. PipelineManagerV2 测试
# ═══════════════════════════════════════════════════════════════


class TestPipelineManagerV2:
    @pytest.mark.asyncio
    async def test_run_full_completes(self, manager):
        result = await manager.run_full(crawl_days=1, task_id="task-v2-test")
        assert result["status"] == "completed"
        assert result["pipeline_id"] == "task-v2-test"

    @pytest.mark.asyncio
    async def test_pipeline_id_in_result(self, manager):
        result = await manager.run_full()
        assert len(result["pipeline_id"]) == 8

    @pytest.mark.asyncio
    async def test_pipeline_without_db(
        self, mock_tools, mock_classifier, mock_scorer, mock_draft_gen, mock_knowledge
    ):
        from agent.pipeline_v2 import PipelineManagerV2

        mgr = PipelineManagerV2(
            tools=mock_tools,
            classifier_v2=mock_classifier,
            scorer_v2=mock_scorer,
            draft_gen=mock_draft_gen,
            knowledge=mock_knowledge,
            db=None,
        )
        result = await mgr.run_full()
        assert result["status"] == "completed"


# ═══════════════════════════════════════════════════════════════
# 3. 节点测试
# ═══════════════════════════════════════════════════════════════


class TestClassifyV2Node:
    @pytest.mark.asyncio
    async def test_classify_v2_no_db(self, mock_classifier):
        from agent.pipeline_v2 import classify_v2_node, create_state_v2

        state = create_state_v2()
        result = await classify_v2_node(state, mock_classifier, None)
        assert result is not None

    @pytest.mark.asyncio
    async def test_classify_v2_no_articles(self, mock_classifier, mock_db):
        from agent.pipeline_v2 import classify_v2_node, create_state_v2

        state = create_state_v2()
        result = await classify_v2_node(state, mock_classifier, mock_db)
        assert result is not None

    @pytest.mark.asyncio
    async def test_classify_v2_with_articles(self, mock_classifier, mock_db):
        from agent.pipeline_v2 import classify_v2_node, create_state_v2

        mock_db["articles"].find.return_value.to_list = AsyncMock(
            return_value=[
                {"_id": "a1", "title": "Test", "pipeline_status": "crawled", "category_v2": ""},
                {"_id": "a2", "title": "Test2", "pipeline_status": "crawled", "category_v2": ""},
            ]
        )

        state = create_state_v2()
        result = await classify_v2_node(state, mock_classifier, mock_db)
        assert result["classified_v2_count"] == 2
        assert result["low_confidence_count"] == 0

    @pytest.mark.asyncio
    async def test_classify_v2_includes_pending_user_uploads(self, mock_classifier, mock_db):
        from agent.pipeline_v2 import classify_v2_node, create_state_v2

        mock_db["articles"].find.return_value.to_list = AsyncMock(
            return_value=[
                {
                    "_id": "upload-1",
                    "url_hash": "a" * 32,
                    "title": "Uploaded article",
                    "source_type": "user_upload",
                    "pipeline_status": "pending",
                    "category_v2": "",
                }
            ]
        )

        result = await classify_v2_node(create_state_v2(), mock_classifier, mock_db)

        query = mock_db["articles"].find.call_args.args[0]
        assert "pending" in query["pipeline_status"]["$in"]
        assert result["classified_v2_count"] == 1


class TestEnrichNode:
    @pytest.mark.asyncio
    async def test_enrich_marks_failed_articles_and_preserves_original_summary(self):
        """Step 8 前向恢复：抓取失败的文章标记 enrich_failed，原摘要保留。"""
        from agent.pipeline_v2 import create_state_v2, enrich_node

        articles = [
            {
                "_id": "a1",
                "url_hash": "h1",
                "url": "https://example.com/1",
                "content_md": "短正文（不足 200 字，作为原始摘要保留）",
            },
            {
                "_id": "a2",
                "url_hash": "h2",
                "url": "https://example.com/2",
                "content_md": "同样短的摘要",
            },
        ]
        articles_col = MagicMock()
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=articles)
        articles_col.find = MagicMock(return_value=cursor)
        articles_col.update_one = AsyncMock()
        pipeline_logs = MagicMock()
        pipeline_logs.insert_one = AsyncMock()
        db = {"articles": articles_col, "pipeline_logs": pipeline_logs}

        crawl_client = MagicMock()
        # 只成功抓取到文章 1；文章 2 抓取失败
        crawl_client.fetch_fulltext_batch = AsyncMock(
            return_value={"https://example.com/1": "x" * 500}
        )

        result = await enrich_node(
            create_state_v2(user_id="user-a"), {}, db, crawl_client
        )

        assert result["enriched_count"] == 1
        assert result["enrich_failed_count"] == 1
        # 文章 1：正文更新；文章 2：仅标记 enrich_failed，不覆盖原摘要
        calls = articles_col.update_one.await_args_list
        content_updates = [
            call.args[1]["$set"].get("content_md")
            for call in calls
            if call.args[1]["$set"].get("content_md")
        ]
        assert len(content_updates) == 1
        assert len(content_updates[0]) == 500
        failed_marks = [
            call.args[1]["$set"]
            for call in calls
            if call.args[1]["$set"].get("enrich_failed") is True
        ]
        assert len(failed_marks) == 1
        assert failed_marks[0]["enrich_failed_reason"] == "enrich_failed"
        # 文章 2 未被写入 content_md
        assert all("content_md" not in mark for mark in failed_marks)

    @pytest.mark.asyncio
    async def test_enrich_batch_failure_marks_all_failed_and_continues(self):
        """Step 8 前向恢复：批量抓取异常时全部标记 enrich_failed，按 policy 继续。"""
        from agent.pipeline_v2 import create_state_v2, enrich_node

        articles = [
            {
                "_id": "a1",
                "url_hash": "h1",
                "url": "https://example.com/1",
                "content_md": "原摘要 1",
            },
            {
                "_id": "a2",
                "url_hash": "h2",
                "url": "https://example.com/2",
                "content_md": "原摘要 2",
            },
        ]
        articles_col = MagicMock()
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=articles)
        articles_col.find = MagicMock(return_value=cursor)
        articles_col.update_one = AsyncMock()
        pipeline_logs = MagicMock()
        pipeline_logs.insert_one = AsyncMock()
        db = {"articles": articles_col, "pipeline_logs": pipeline_logs}

        crawl_client = MagicMock()
        crawl_client.fetch_fulltext_batch = AsyncMock(side_effect=RuntimeError("crawl down"))

        result = await enrich_node(
            create_state_v2(user_id="user-a"), {}, db, crawl_client
        )

        assert result["enrich_failed_count"] == 2
        assert any("enrich" in error for error in result["errors"])
        marks = [
            call.args[1]["$set"]
            for call in articles_col.update_one.await_args_list
            if call.args[1]["$set"].get("enrich_failed") is True
        ]
        assert len(marks) == 2
        assert all(mark["enrich_failed_reason"] == "enrich_batch_failed" for mark in marks)

    def test_incomplete_article_query_excludes_enrich_failed(self):
        """已标记 enrich_failed 的文章不再进入补爬候选。"""
        from agent.pipeline_v2 import _incomplete_article_query

        query = _incomplete_article_query()
        assert query["enrich_failed"] == {"$ne": True}


class TestScoreV2Node:
    @pytest.mark.asyncio
    async def test_score_v2_no_db(self, mock_scorer, mock_knowledge):
        from agent.pipeline_v2 import create_state_v2, score_v2_node

        state = create_state_v2()
        result = await score_v2_node(state, mock_scorer, mock_knowledge, None)
        assert result is not None

    @pytest.mark.asyncio
    async def test_score_v2_no_articles(self, mock_scorer, mock_knowledge, mock_db):
        from agent.pipeline_v2 import create_state_v2, score_v2_node

        state = create_state_v2()
        result = await score_v2_node(state, mock_scorer, mock_knowledge, mock_db)
        assert result is not None

    @pytest.mark.asyncio
    async def test_score_v2_with_articles(self, mock_scorer, mock_knowledge, mock_db):
        from agent.pipeline_v2 import create_state_v2, score_v2_node

        mock_db["articles"].find.return_value.to_list = AsyncMock(
            return_value=[
                {"_id": "a1", "title": "Test", "is_pr_eligible": True},
                {"_id": "a2", "title": "Test2", "is_pr_eligible": True},
            ]
        )

        state = create_state_v2()
        result = await score_v2_node(state, mock_scorer, mock_knowledge, mock_db)
        assert result["scored_v2_count"] == 2
        mock_scorer.adjust_threshold.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_score_v2_uses_adjusted_threshold_state(
        self, mock_scorer, mock_knowledge, mock_db
    ):
        from agent.pipeline_v2 import create_state_v2, score_v2_node

        mock_scorer.adjust_threshold = AsyncMock(
            return_value={
                "base_threshold": 80,
                "adjustment": 6,
                "threshold": 86,
                "feedback_count": 4,
                "directional_count": 3,
            }
        )
        mock_db["articles"].find.return_value.to_list = AsyncMock(
            return_value=[
                {"_id": "a1", "title": "Test", "is_pr_eligible": True},
            ]
        )

        state = create_state_v2()
        result = await score_v2_node(state, mock_scorer, mock_knowledge, mock_db)
        assert result["score_threshold"] == 86
        assert result["threshold_adjustment"] == 6


class TestDraftNode:
    @pytest.mark.asyncio
    async def test_draft_no_db(self, mock_draft_gen, mock_knowledge):
        from agent.pipeline_v2 import create_state_v2, draft_node

        state = create_state_v2()
        result = await draft_node(state, mock_draft_gen, mock_knowledge, None)
        assert result is not None

    @pytest.mark.asyncio
    async def test_draft_no_articles(self, mock_draft_gen, mock_knowledge, mock_db):
        from agent.pipeline_v2 import create_state_v2, draft_node

        state = create_state_v2()
        result = await draft_node(state, mock_draft_gen, mock_knowledge, mock_db)
        assert result is not None

    @pytest.mark.asyncio
    async def test_draft_with_high_score_articles(self, mock_draft_gen, mock_knowledge, mock_db):
        from agent.pipeline_v2 import create_state_v2, draft_node

        mock_db["articles"].find.return_value.to_list = AsyncMock(
            return_value=[
                {
                    "_id": "a1",
                    "url_hash": "a" * 32,
                    "title": "Test",
                    "pr_total_score": 157,
                    "product_relevance": 85,
                    "event_impact": 72,
                },
            ]
        )

        state = create_state_v2()
        result = await draft_node(state, mock_draft_gen, mock_knowledge, mock_db)
        assert result["draft_count"] == 1

    @pytest.mark.asyncio
    async def test_draft_injects_style_hints_when_profile_exists(
        self, mock_draft_gen, mock_knowledge
    ):
        from agent.pipeline_v2 import create_state_v2, draft_node

        article_collection = MagicMock()
        article_collection.find.return_value.to_list = AsyncMock(
            return_value=[
                {
                    "_id": "a1",
                    "url_hash": "a" * 32,
                    "title": "Test",
                    "pr_total_score": 157,
                    "product_relevance": 85,
                    "event_impact": 72,
                },
            ]
        )
        article_collection.update_one = AsyncMock()
        profile_collection = MagicMock()
        profile_collection.find_one = AsyncMock(
            return_value={
                "user_id": "local-user",
                "style_hints": {
                    "preferred_templates": ["爆点A"],
                    "preferred_perspectives": ["市场传播视角"],
                    "preferred_tone": "market_oriented",
                    "preferred_length": "medium",
                    "common_revise_directions": ["增强传播性"],
                    "avoid_patterns": [],
                },
            }
        )
        user_drafts_collection = MagicMock()
        user_drafts_collection.update_one = AsyncMock()
        db = {
            "articles": article_collection,
            "user_profiles": profile_collection,
            "user_drafts": user_drafts_collection,
        }

        state = create_state_v2(user_id="local-user")
        result = await draft_node(state, mock_draft_gen, mock_knowledge, db)
        assert result["draft_count"] == 1
        call = mock_draft_gen.generate.await_args
        assert "用户风格偏好" in call.kwargs["style_hints"]
        assert "爆点A" in call.kwargs["style_hints"]
        user_drafts_collection.update_one.assert_awaited_once()
        assert user_drafts_collection.update_one.await_args.args[0] == {
            "user_id": "local-user",
            "article_url_hash": "a" * 32,
        }
        assert user_drafts_collection.update_one.await_args.kwargs == {"upsert": True}


class TestReviewNode:
    @pytest.mark.asyncio
    async def test_missing_reviewer_stores_failed_status_instead_of_losing_draft(self):
        from agent.pipeline_v2 import create_state_v2, review_node

        user_drafts = MagicMock()
        user_drafts.find.return_value.to_list = AsyncMock(
            return_value=[
                {
                    "article_url_hash": "hash-1",
                    "drafts": [{"title": "Draft", "content_md": "正文"}],
                }
            ]
        )
        user_drafts.update_one = AsyncMock()
        articles = MagicMock()
        articles.find_one = AsyncMock(return_value={"content_md": "原文"})
        pipeline_logs = MagicMock()
        pipeline_logs.insert_one = AsyncMock()
        db = {
            "user_drafts": user_drafts,
            "articles": articles,
            "pipeline_logs": pipeline_logs,
        }

        result = await review_node(create_state_v2(user_id="user-a"), None, db)

        assert result["review_count"] == 1
        assert result["review_failed_count"] == 1
        assert result["review_pending_count"] == 1
        stored = next(
            call.args[1]["$set"]["drafts.0.review"]
            for call in user_drafts.update_one.await_args_list
            if "drafts.0.review" in call.args[1]["$set"]
        )
        assert stored["status"] == "failed"
        assert stored["error"] == "Draft reviewer not initialized"
        assert any(
            call.args[1]["$set"].get("review_status") == "pending_review"
            for call in user_drafts.update_one.await_args_list
        )

    @pytest.mark.asyncio
    async def test_review_limits_concurrency_reuses_hash_and_persists_results(self):
        import asyncio

        from agent.draft_reviewer import compute_content_hash
        from agent.pipeline_v2 import create_state_v2, review_node
        from models.draft_review import DraftReview

        drafts = [
            {"title": f"Draft {index}", "content_md": f"正文 {index}"} for index in range(4)
        ]
        drafts[0]["review"] = {
            "status": "completed",
            "content_hash": compute_content_hash(drafts[0]["content_md"]),
        }
        user_drafts = MagicMock()
        user_drafts.find.return_value.to_list = AsyncMock(
            return_value=[{"article_url_hash": "hash-1", "drafts": drafts}]
        )
        user_drafts.update_one = AsyncMock()
        articles = MagicMock()
        articles.find_one = AsyncMock(
            return_value={"title": "Source", "content_md": "原文内容"}
        )
        pipeline_logs = MagicMock()
        pipeline_logs.insert_one = AsyncMock()
        db = {
            "user_drafts": user_drafts,
            "articles": articles,
            "pipeline_logs": pipeline_logs,
        }

        active = 0
        max_active = 0

        async def review(_article, draft):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return DraftReview(
                status="completed",
                content_hash=compute_content_hash(draft["content_md"]),
                summary="未发现问题",
                issues=[],
                counts={"high": 0, "medium": 0, "low": 0},
                fact_check_available=True,
            )

        reviewer = MagicMock()
        reviewer.review = AsyncMock(side_effect=review)
        result = await review_node(create_state_v2(user_id="user-a"), reviewer, db)

        assert result["review_count"] == 3
        assert result["review_reused_count"] == 1
        assert result["review_failed_count"] == 0
        assert max_active == 2
        assert user_drafts.update_one.await_count == 3
        persisted_paths = {
            next(key for key in call.args[1]["$set"] if key.startswith("drafts."))
            for call in user_drafts.update_one.await_args_list
        }
        assert persisted_paths == {
            "drafts.1.review",
            "drafts.2.review",
            "drafts.3.review",
        }

    @pytest.mark.asyncio
    async def test_review_failure_is_stored_without_stopping_other_drafts(self):
        from agent.draft_reviewer import compute_content_hash
        from agent.pipeline_v2 import create_state_v2, review_node
        from models.draft_review import DraftReview

        drafts = [
            {"title": "Bad", "content_md": "失败稿件"},
            {"title": "Good", "content_md": "正常稿件"},
        ]
        user_drafts = MagicMock()
        user_drafts.find.return_value.to_list = AsyncMock(
            return_value=[{"article_url_hash": "hash-1", "drafts": drafts}]
        )
        user_drafts.update_one = AsyncMock()
        articles = MagicMock()
        articles.find_one = AsyncMock(return_value={"content_md": "原文"})
        pipeline_logs = MagicMock()
        pipeline_logs.insert_one = AsyncMock()
        db = {
            "user_drafts": user_drafts,
            "articles": articles,
            "pipeline_logs": pipeline_logs,
        }

        async def review(_article, draft):
            if draft["title"] == "Bad":
                raise RuntimeError("review unavailable")
            return DraftReview(
                status="completed",
                content_hash=compute_content_hash(draft["content_md"]),
                summary="未发现问题",
                issues=[],
                counts={"high": 0, "medium": 0, "low": 0},
                fact_check_available=True,
            )

        reviewer = MagicMock()
        reviewer.review = AsyncMock(side_effect=review)
        result = await review_node(create_state_v2(user_id="user-a"), reviewer, db)

        assert result["review_count"] == 2
        assert result["review_failed_count"] == 1
        stored_statuses = [
            next(
                value["status"]
                for key, value in call.args[1]["$set"].items()
                if key.startswith("drafts.")
            )
            for call in user_drafts.update_one.await_args_list
            if any(key.startswith("drafts.") for key in call.args[1]["$set"])
        ]
        assert sorted(stored_statuses) == ["completed", "failed"]

    @pytest.mark.asyncio
    async def test_review_failure_marks_document_pending_review(self):
        """Step 8 前向恢复：任一草稿评审失败 → 文档 review_status=pending_review（禁止发布）。"""
        from agent.draft_reviewer import compute_content_hash
        from agent.pipeline_v2 import create_state_v2, review_node
        from models.draft_review import DraftReview

        drafts = [
            {"title": "Bad", "content_md": "失败稿件"},
            {"title": "Good", "content_md": "正常稿件"},
        ]
        user_drafts = MagicMock()
        user_drafts.find.return_value.to_list = AsyncMock(
            return_value=[{"article_url_hash": "hash-1", "drafts": drafts}]
        )
        user_drafts.update_one = AsyncMock()
        articles = MagicMock()
        articles.find_one = AsyncMock(return_value={"content_md": "原文"})
        pipeline_logs = MagicMock()
        pipeline_logs.insert_one = AsyncMock()
        db = {
            "user_drafts": user_drafts,
            "articles": articles,
            "pipeline_logs": pipeline_logs,
        }

        async def review(_article, draft):
            if draft["title"] == "Bad":
                raise RuntimeError("review unavailable")
            return DraftReview(
                status="completed",
                content_hash=compute_content_hash(draft["content_md"]),
                summary="未发现问题",
                issues=[],
                counts={"high": 0, "medium": 0, "low": 0},
                fact_check_available=True,
            )

        reviewer = MagicMock()
        reviewer.review = AsyncMock(side_effect=review)
        result = await review_node(create_state_v2(user_id="user-a"), reviewer, db)

        assert result["review_pending_count"] == 1
        pending_sets = [
            call.args[1]["$set"]
            for call in user_drafts.update_one.await_args_list
            if call.args[1]["$set"].get("review_status") == "pending_review"
        ]
        assert len(pending_sets) == 1
        assert pending_sets[0]["review_status"] == "pending_review"


# ═══════════════════════════════════════════════════════════════
# 4. 全流程 E2E 集成测试
# ═══════════════════════════════════════════════════════════════


class TestPipelineV2E2E:
    """V2 流水线全流程 mock 验证"""

    @pytest.fixture
    def e2e_db(self):
        """模拟含文章数据的 MongoDB"""
        db = MagicMock()
        articles_mock = MagicMock()
        db.__getitem__ = MagicMock(return_value=articles_mock)

        # 内存存储
        store: list[dict] = []

        async def _to_list(length=100):
            return store

        async def _update_one(filter_dict, update_dict, **kwargs):
            for a in store:
                if a.get("_id") == filter_dict.get("_id"):
                    if "$set" in update_dict:
                        a.update(update_dict["$set"])
                    return MagicMock(modified_count=1)
            return MagicMock(modified_count=0)

        async def _count_documents(query):
            if "$expr" in query:
                return 0
            if query.get("is_pr_eligible") is True:
                return sum(1 for article in store if article.get("is_pr_eligible") is True)
            return len(store)

        mock_cursor = MagicMock()
        mock_cursor.to_list = _to_list
        articles_mock.find = MagicMock(return_value=mock_cursor)
        articles_mock.find_one = AsyncMock(return_value=None)
        articles_mock.insert_one = AsyncMock()
        articles_mock.update_one = _update_one
        articles_mock.count_documents = _count_documents

        return db, store

    @pytest.mark.asyncio
    async def test_full_v2_pipeline_e2e(
        self,
        mock_tools,
        mock_classifier,
        mock_scorer,
        mock_draft_gen,
        mock_knowledge,
        e2e_db,
    ):
        """验证 V2 流水线 4 阶段完整通过"""
        from agent.pipeline_v2 import PipelineManagerV2

        db, store = e2e_db

        # 初始化数据：2 篇 crawled 文章
        store.extend(
            [
                {
                    "_id": "a1",
                    "url_hash": "a" * 32,
                    "title": "MCP RCE Vulnerability",
                    "pipeline_status": "crawled",
                    "category_v2": "",
                },
                {
                    "_id": "a2",
                    "url_hash": "b" * 32,
                    "title": "New AI Regulation",
                    "pipeline_status": "crawled",
                    "category_v2": "",
                },
            ]
        )

        manager = PipelineManagerV2(
            tools=mock_tools,
            classifier_v2=mock_classifier,
            scorer_v2=mock_scorer,
            draft_gen=mock_draft_gen,
            knowledge=mock_knowledge,
            db=db,
        )

        result = await manager.run_full()
        assert result["status"] == "completed"
        assert result["state"]["classified_v2_count"] == 2
        assert result["state"]["scored_v2_count"] == 2
        assert result["state"]["draft_count"] >= 1

    @pytest.mark.asyncio
    async def test_no_pr_eligible_articles_skips_scoring(
        self,
        mock_tools,
        mock_knowledge,
        e2e_db,
    ):
        """没有 PR 候选文章时，score 和 draft 跳过"""
        from agent.pipeline_v2 import PipelineManagerV2

        db, store = e2e_db
        store.append(
            {
                "_id": "a1",
                "url_hash": "a" * 32,
                "title": "Competitor News",
                "pipeline_status": "crawled",
                "category_v2": "",
            },
        )

        # Mock classifier 返回非 PR 类别
        from agent.classifier_v2 import ClassifyResultV2

        classifier = MagicMock()
        classifier.classify_batch = AsyncMock(
            return_value=[
                ClassifyResultV2(category="国内外竞品信息", confidence=80, reason="友商动态"),
            ]
        )

        scorer = MagicMock()
        scorer.score_batch = AsyncMock(return_value=[])

        draft_gen = MagicMock()
        draft_gen.generate = AsyncMock(return_value={"ok": False, "drafts": [], "error": "none"})

        manager = PipelineManagerV2(
            tools=mock_tools,
            classifier_v2=classifier,
            scorer_v2=scorer,
            draft_gen=draft_gen,
            knowledge=mock_knowledge,
            db=db,
        )

        result = await manager.run_full()
        assert result["status"] == "completed"
        assert result["state"]["classified_v2_count"] == 1
        assert result["state"]["pr_eligible_count"] == 0
        assert result["state"]["scored_v2_count"] == 0
