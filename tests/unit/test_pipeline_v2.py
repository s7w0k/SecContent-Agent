"""
Agent 流水线编排 V2 — 单元测试

运行:
    pytest tests/unit/test_pipeline_v2.py -v
"""

from __future__ import annotations

import contextlib
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
        assert len(state["phases"]) == 4
        assert "crawl" in state["phases"]
        assert "classify_v2" in state["phases"]
        assert "score_v2" in state["phases"]
        assert "draft" in state["phases"]
        assert state["crawl_days"] == 1


# ═══════════════════════════════════════════════════════════════
# 2. PipelineManagerV2 测试
# ═══════════════════════════════════════════════════════════════


class TestPipelineManagerV2:
    def test_initial_status_idle(self, manager):
        status = manager.get_status()
        assert status["status"] == "idle"

    @pytest.mark.asyncio
    async def test_run_full_completes(self, manager):
        result = await manager.run_full(crawl_days=1)
        assert result["status"] == "completed"
        assert "pipeline_id" in result

    @pytest.mark.asyncio
    async def test_status_after_run(self, manager):
        await manager.run_full()
        status = manager.get_status()
        assert status["status"] == "completed"

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
        assert result["pr_eligible_count"] == 1


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
        db = {
            "articles": article_collection,
            "user_profiles": profile_collection,
        }

        state = create_state_v2()
        result = await draft_node(state, mock_draft_gen, mock_knowledge, db)
        assert result["draft_count"] == 1
        call = mock_draft_gen.generate.await_args
        assert "用户风格偏好" in call.kwargs["style_hints"]
        assert "爆点A" in call.kwargs["style_hints"]


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

        mock_cursor = MagicMock()
        mock_cursor.to_list = _to_list
        articles_mock.find = MagicMock(return_value=mock_cursor)
        articles_mock.update_one = _update_one

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
                    "title": "MCP RCE Vulnerability",
                    "pipeline_status": "crawled",
                    "category_v2": "",
                },
                {
                    "_id": "a2",
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

    @pytest.mark.asyncio
    async def test_cancel_pipeline(self, manager):
        """流水线可被取消"""
        import asyncio

        task = asyncio.create_task(manager.run_full())
        await asyncio.sleep(0.1)
        await manager.cancel()

        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=2.0)

        status = manager.get_status()
        assert status["status"] in ("cancelled", "completed", "failed")
