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
    classifier.classify_batch = AsyncMock(return_value=[
        ClassifyResultV2(category="爆点事件", confidence=90, reason="重大漏洞"),
        ClassifyResultV2(category="国内外竞品信息", confidence=80, reason="友商动态"),
    ])
    return classifier


@pytest.fixture
def mock_scorer():
    """Mock ScoringAgentV2"""
    scorer = MagicMock()
    scorer.score_batch = AsyncMock(return_value=[
        {"product_relevance": 85, "event_impact": 72, "pr_total_score": 157, "is_pr_candidate": True, "_fallback": False},
        {"product_relevance": 25, "event_impact": 30, "pr_total_score": 55, "is_pr_candidate": False, "_fallback": False},
    ])
    return scorer


@pytest.fixture
def mock_draft_gen():
    """Mock DraftGenerator"""
    gen = MagicMock()
    gen.generate = AsyncMock(return_value={
        "ok": True,
        "drafts": [
            {"template": "爆点A", "perspective": "角度1", "content_md": "# Draft", "title": "T", "index": 1},
        ],
        "error": None,
    })
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
    async def test_pipeline_without_db(self, mock_tools, mock_classifier, mock_scorer, mock_draft_gen, mock_knowledge):
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

        mock_db["articles"].find.return_value.to_list = AsyncMock(return_value=[
            {"_id": "a1", "title": "Test", "pipeline_status": "crawled", "category_v2": ""},
            {"_id": "a2", "title": "Test2", "pipeline_status": "crawled", "category_v2": ""},
        ])

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

        mock_db["articles"].find.return_value.to_list = AsyncMock(return_value=[
            {"_id": "a1", "title": "Test", "is_pr_eligible": True},
            {"_id": "a2", "title": "Test2", "is_pr_eligible": True},
        ])

        state = create_state_v2()
        result = await score_v2_node(state, mock_scorer, mock_knowledge, mock_db)
        assert result["scored_v2_count"] == 2


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

        mock_db["articles"].find.return_value.to_list = AsyncMock(return_value=[
            {"_id": "a1", "title": "Test", "pr_total_score": 157, "product_relevance": 85, "event_impact": 72},
        ])

        state = create_state_v2()
        result = await draft_node(state, mock_draft_gen, mock_knowledge, mock_db)
        assert result["draft_count"] == 1
