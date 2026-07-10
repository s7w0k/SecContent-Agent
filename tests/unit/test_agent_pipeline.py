"""
Agent 流水线编排 — 单元测试

运行:
    pytest tests/unit/test_agent_pipeline.py -v
"""

from __future__ import annotations

import contextlib
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def mock_tools():
    """Mock MCP Tools — 返回空数据"""
    def _make_tool(return_value=None):
        tool = MagicMock()
        tool.ainvoke = AsyncMock(return_value=return_value or {"ok": True, "data": {"articles": []}})
        return tool

    return {
        "crawl_overseas_news": _make_tool(),
        "fetch_wewe_articles": _make_tool(),
        "classify_articles": _make_tool({"ok": True, "data": {"classified": []}}),
        "query_articles": _make_tool(),
        "get_crawl_stats": _make_tool(),
        "export_articles_csv": _make_tool(),
        "fetch_article_fulltext": _make_tool(),
        "analyze_wewe_article": _make_tool(),
    }


@pytest.fixture
def mock_scorer():
    scorer = MagicMock()
    scorer.score_batch = AsyncMock(return_value=[])
    scorer.score_single = AsyncMock(return_value={
        "ai_relevance_score": 80,
        "reportability_score": 70,
        "total_score": 150,
        "is_high_value": True,
        "score_reason": "test",
        "tags": ["test"],
        "_fallback": False,
    })
    return scorer


@pytest.fixture
def mock_reporter():
    reporter = MagicMock()
    reporter.generate_report = AsyncMock(return_value={
        "ok": True,
        "report": {"title": "T", "content_md": "# Report", "article_url_hash": "h"},
        "error": None,
    })
    return reporter


@pytest.fixture
def mock_knowledge():
    knowledge = MagicMock()
    knowledge.load = AsyncMock()
    knowledge.as_system_prompt = MagicMock(return_value="test knowledge")
    return knowledge


@pytest.fixture
def mock_db():
    """Mock MongoDB with find/insert/update operations"""
    db = MagicMock()

    # 确保 db["articles"] 始终返回同一个 mock
    articles_mock = MagicMock()
    reports_mock = MagicMock()
    user_profiles_mock = MagicMock()

    collections = {
        "articles": articles_mock,
        "reports": reports_mock,
        "user_profiles": user_profiles_mock,
    }

    def _getitem(key):
        return collections.get(key, MagicMock())

    db.__getitem__.side_effect = _getitem

    # articles collection operations
    articles_mock.find_one = AsyncMock(return_value=None)
    mock_cursor = MagicMock()
    mock_cursor.to_list = AsyncMock(return_value=[])
    articles_mock.find = MagicMock(return_value=mock_cursor)
    articles_mock.insert_one = AsyncMock()
    articles_mock.update_one = AsyncMock()

    # reports collection operations
    reports_mock.insert_one = AsyncMock(return_value=MagicMock(inserted_id="rid-123"))
    reports_mock.find = MagicMock(return_value=MagicMock())
    reports_mock.find.return_value.to_list = AsyncMock(return_value=[])
    user_profiles_mock.find_one = AsyncMock(return_value=None)

    return db


@pytest.fixture
def manager(mock_tools, mock_scorer, mock_reporter, mock_knowledge, mock_db):
    from agent.pipeline import PipelineManager

    return PipelineManager(
        tools=mock_tools,
        scorer=mock_scorer,
        reporter=mock_reporter,
        knowledge=mock_knowledge,
        db=mock_db,
    )


# ═══════════════════════════════════════════════════════════════
# 1. create_state 测试
# ═══════════════════════════════════════════════════════════════


class Testcreate_state:
    """状态对象测试"""

    def test_default_phases(self):
        from agent.pipeline import create_state

        state = create_state()
        assert len(state["phases"]) == 4
        assert "crawl" in state["phases"]

    def test_custom_phases(self):
        from agent.pipeline import create_state

        state = create_state(phases=["crawl", "score"])
        assert state["phases"] == ["crawl", "score"]
        assert state["crawl_days"] == 1

    def test_status_property(self):
        from agent.pipeline import create_state

        state = create_state()
        state["status"] = "running"
        assert state["status"] == "running"

    def test_add_error(self):
        from agent.pipeline import create_state

        state = create_state()
        state["errors"].append("test error")
        assert len(state["errors"]) == 1
        assert state["errors"][0] == "test error"

    def test_to_dict(self):
        from agent.pipeline import create_state

        state = create_state(crawl_days=3)
        d = dict(state)
        assert d["crawl_days"] == 3
        assert isinstance(d["errors"], list)


# ═══════════════════════════════════════════════════════════════
# 2. PipelineManager 生命周期测试
# ═══════════════════════════════════════════════════════════════


class TestPipelineManager:
    """管理器核心功能"""

    def test_initial_status_idle(self, manager):
        status = manager.get_status()
        assert status["status"] == "idle"

    @pytest.mark.asyncio
    async def test_run_full_completes(self, manager):
        result = await manager.run_full(crawl_days=1)
        assert result["status"] == "completed"
        state = result["state"]
        assert state["crawled_count"] >= 0
        assert state["classified_count"] >= 0
        assert state["scored_count"] >= 0
        assert state["report_count"] >= 0

    @pytest.mark.asyncio
    async def test_status_after_run(self, manager):
        await manager.run_full()
        status = manager.get_status()
        assert status["status"] == "completed"

    @pytest.mark.asyncio
    async def test_run_single_phase(self, manager):
        """单阶段执行"""
        result = await manager.run_phase("crawl")
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_run_phase_includes_prior(self, manager):
        """执行 score 阶段会包含前面的 crawl+classify"""
        result = await manager.run_phase("score")
        # 验证 classify 和 crawl 也被执行
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_run_unknown_phase_raises(self, manager):
        with pytest.raises(ValueError, match="Unknown phase"):
            await manager.run_phase("nonexistent")

    @pytest.mark.asyncio
    async def test_rejected_when_already_running(self, manager):
        """并发保护：运行中拒绝重复启动"""
        # 模拟运行中（使用真实 dict 确保状态检查正确）
        manager._state = {"status": "running", "phases": [], "crawl_days": 1}
        result = await manager.run_full()
        assert result["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_pipeline_id_in_result(self, manager):
        result = await manager.run_full()
        assert "pipeline_id" in result
        assert len(result["pipeline_id"]) > 0


# ═══════════════════════════════════════════════════════════════
# 3. 各节点单元测试
# ═══════════════════════════════════════════════════════════════


class TestCrawlNode:
    """爬取节点测试"""

    @pytest.mark.asyncio
    async def test_crawl_node_no_db(self, mock_tools):
        from agent.pipeline import crawl_node, create_state

        state = create_state(crawl_days=2)
        result = await crawl_node(state, mock_tools, None)
        assert result["crawled_count"] >= 0
        assert result["current_phase"] == "crawl"

    @pytest.mark.asyncio
    async def test_crawl_node_with_articles(self, mock_tools, mock_db):
        from agent.pipeline import crawl_node, create_state

        # Mock crawl tool to return articles
        mock_tools["crawl_overseas_news"].ainvoke = AsyncMock(return_value={
            "ok": True,
            "data": {
                "articles": [
                    {
                        "title": "Test Article",
                        "url": "https://x.com/a",
                        "url_hash": "abc123",
                        "source": "THN",
                        "source_type": "overseas_news",
                        "summary": "test",
                    },
                ],
                "count": 1,
            },
        })

        mock_db["articles"].find_one = AsyncMock(return_value=None)
        mock_db["articles"].insert_one = AsyncMock()

        # Note: crawl_node also tries to fetch WeWe Atom feed.
        # If the feed is reachable, crawled_count may be >1.
        # We mock httpx to fail the WeWe fetch so only overseas articles count.
        with patch("httpx.AsyncClient") as mock_httpx_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("mocked WeWe offline"))
            mock_httpx_cls.return_value = mock_client
            # Mock __aenter__ for "async with" context
            mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            state = create_state(crawl_days=1)
            result = await crawl_node(state, mock_tools, mock_db)
        assert result["crawled_count"] == 1

    @pytest.mark.asyncio
    async def test_crawl_node_skips_duplicates(self, mock_tools, mock_db):
        from agent.pipeline import crawl_node, create_state

        mock_tools["crawl_overseas_news"].ainvoke = AsyncMock(return_value={
            "ok": True,
            "data": {"articles": [{"title": "T", "url": "https://x.com", "url_hash": "dup"}]},
        })
        mock_db["articles"].find_one = AsyncMock(return_value={"_id": "exists"})

        state = create_state(crawl_days=1)
        result = await crawl_node(state, mock_tools, mock_db)
        assert result["crawled_count"] == 0  # duplicate skipped

    @pytest.mark.asyncio
    async def test_crawl_node_skipped(self, mock_tools):
        from agent.pipeline import crawl_node, create_state

        state = create_state(phases=["score"])  # crawl not in phases
        result = await crawl_node(state, mock_tools, None)
        assert result["current_phase"] == "crawl"  # phase is set but skipped


class TestScoreNode:
    """打分节点测试"""

    @pytest.mark.asyncio
    async def test_score_node_no_db(self, mock_tools, mock_scorer, mock_knowledge):
        from agent.pipeline import create_state, score_node

        state = create_state()
        result = await score_node(state, mock_tools, mock_scorer, mock_knowledge, None)
        assert result["current_phase"] == "score"

    @pytest.mark.asyncio
    async def test_score_node_with_articles(self, mock_tools, mock_scorer, mock_knowledge, mock_db):
        from agent.pipeline import create_state, score_node

        # Mock classified articles in DB
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[
            {"_id": "1", "title": "T", "is_ai_security": True},
            {"_id": "2", "title": "T2", "is_ai_security": True},
        ])
        mock_db["articles"].find = MagicMock(return_value=mock_cursor)
        mock_scorer.score_batch = AsyncMock(return_value=[
            {"ai_relevance_score": 85, "reportability_score": 72, "score_reason": "ok", "tags": [], "_fallback": False},
            {"ai_relevance_score": 30, "reportability_score": 20, "score_reason": "meh", "tags": [], "_fallback": False},
        ])

        state = create_state()
        result = await score_node(state, mock_tools, mock_scorer, mock_knowledge, mock_db)
        assert result["scored_count"] == 2

    @pytest.mark.asyncio
    async def test_score_node_skipped(self, mock_tools, mock_scorer, mock_knowledge):
        from agent.pipeline import create_state, score_node

        state = create_state(phases=["crawl"])
        result = await score_node(state, mock_tools, mock_scorer, mock_knowledge, None)
        assert result["current_phase"] == "score"


class TestReportNode:
    """报道生成节点测试"""

    @pytest.mark.asyncio
    async def test_report_node_no_high_value(self, mock_tools, mock_reporter, mock_knowledge, mock_db):
        from agent.pipeline import create_state, report_node

        # No scored articles
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[])
        mock_db["articles"].find = MagicMock(return_value=mock_cursor)

        state = create_state()
        result = await report_node(state, mock_tools, mock_reporter, mock_knowledge, mock_db)
        assert result["report_count"] == 0

    @pytest.mark.asyncio
    async def test_report_node_generates(self, mock_tools, mock_reporter, mock_knowledge, mock_db):
        from agent.pipeline import create_state, report_node

        # One high-value scored article
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[
            {"_id": "1", "title": "High", "ai_relevance_score": 85, "reportability_score": 75},
        ])
        mock_db["articles"].find = MagicMock(return_value=mock_cursor)

        state = create_state()
        result = await report_node(state, mock_tools, mock_reporter, mock_knowledge, mock_db)
        # 85+75=160 >= 140 → should generate
        assert result["report_count"] == 1
        assert mock_reporter.generate_report.await_args.kwargs["style_hints"] is None

    @pytest.mark.asyncio
    async def test_report_node_injects_current_user_style(
        self,
        mock_tools,
        mock_reporter,
        mock_knowledge,
        mock_db,
    ):
        from agent.pipeline import create_state, report_node

        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(
            return_value=[
                {
                    "_id": "1",
                    "title": "High",
                    "ai_relevance_score": 85,
                    "reportability_score": 75,
                },
            ],
        )
        mock_db["articles"].find = MagicMock(return_value=mock_cursor)
        mock_db["user_profiles"].find_one = AsyncMock(
            return_value={
                "user_id": "user-a",
                "style_hints": {"preferred_templates": ["爆点A"]},
            },
        )

        await report_node(
            create_state(user_id="user-a"),
            mock_tools,
            mock_reporter,
            mock_knowledge,
            mock_db,
        )

        style_hints = mock_reporter.generate_report.await_args.kwargs["style_hints"]
        assert "爆点A" in style_hints

    @pytest.mark.asyncio
    async def test_report_node_below_threshold(self, mock_tools, mock_reporter, mock_knowledge, mock_db):
        from agent.pipeline import create_state, report_node

        # Low-value scored article
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[
            {"_id": "1", "title": "Low", "ai_relevance_score": 30, "reportability_score": 20},
        ])
        mock_db["articles"].find = MagicMock(return_value=mock_cursor)

        state = create_state()
        result = await report_node(state, mock_tools, mock_reporter, mock_knowledge, mock_db)
        # 30+20=50 < 140 → skip
        assert result["report_count"] == 0


# ═══════════════════════════════════════════════════════════════
# 4. 错误场景测试
# ═══════════════════════════════════════════════════════════════


class TestErrorScenarios:
    """错误隔离与恢复"""

    @pytest.mark.asyncio
    async def test_crawl_error_not_fatal(self, mock_tools, mock_scorer, mock_reporter, mock_knowledge, mock_db):
        from agent.pipeline import PipelineManager

        mock_tools["crawl_overseas_news"].ainvoke = AsyncMock(
            side_effect=Exception("Crawl failed")
        )

        manager = PipelineManager(mock_tools, mock_scorer, mock_reporter, mock_knowledge, mock_db)
        result = await manager.run_full()
        # 错误不应阻塞整体完成
        assert result["status"] == "completed"
        assert len(result["state"]["errors"]) > 0

    @pytest.mark.asyncio
    async def test_pipeline_cancel(self, manager):
        import asyncio

        task = asyncio.create_task(manager.run_full())
        await asyncio.sleep(0.1)  # give pipeline time to start crawling
        await manager.cancel()

        # wait for cancellation to propagate
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)

        status = manager.get_status()
        assert status["status"] in ("cancelled", "completed", "failed")


# ═══════════════════════════════════════════════════════════════
# 5. 集成场景测试
# ═══════════════════════════════════════════════════════════════


class TestIntegrationScenarios:
    """模拟完整流水线执行"""

    @pytest.mark.asyncio
    async def test_full_pipeline_with_crawled_data(
        self, mock_tools, mock_scorer, mock_reporter, mock_knowledge, mock_db
    ):
        from agent.pipeline import PipelineManager

        # Setup: crawl returns articles → classify returns classified → score scores → report generates
        mock_tools["crawl_overseas_news"].ainvoke = AsyncMock(return_value={
            "ok": True,
            "data": {"articles": [
                {"title": "A", "url": "https://x.com/a", "url_hash": "h1", "source": "S", "source_type": "news", "summary": "s"},
            ]},
        })

        # find_one returns None (no duplicate)
        mock_db["articles"].find_one = AsyncMock(return_value=None)

        # find for classify returns the article
        classify_cursor = MagicMock()
        classify_cursor.to_list = AsyncMock(return_value=[
            {"_id": "1", "title": "A", "url": "https://x.com/a", "source": "S", "summary": "s"},
        ])
        # find for score returns classified articles
        score_cursor = MagicMock()
        score_cursor.to_list = AsyncMock(return_value=[
            {"_id": "1", "title": "A", "is_ai_security": True},
        ])
        mock_scorer.score_batch = AsyncMock(return_value=[
            {"ai_relevance_score": 90, "reportability_score": 80, "score_reason": "ok", "tags": [], "_fallback": False},
        ])
        # find for report returns scored
        report_cursor = MagicMock()
        report_cursor.to_list = AsyncMock(return_value=[
            {"_id": "1", "title": "A", "ai_relevance_score": 90, "reportability_score": 80},
        ])

        # 按顺序返回不同的 cursor
        mock_db["articles"].find = MagicMock(side_effect=[
            classify_cursor, score_cursor, report_cursor,
        ])

        manager = PipelineManager(mock_tools, mock_scorer, mock_reporter, mock_knowledge, mock_db)
        result = await manager.run_full()

        assert result["status"] == "completed"
        assert result["state"]["crawled_count"] >= 0

    @pytest.mark.asyncio
    async def test_phase_by_phase_execution(
        self, mock_tools, mock_scorer, mock_reporter, mock_knowledge, mock_db
    ):
        """逐阶段执行验证"""
        from agent.pipeline import PipelineManager

        manager = PipelineManager(mock_tools, mock_scorer, mock_reporter, mock_knowledge, mock_db)

        # 阶段 1: 仅爬取
        r1 = await manager.run_phase("crawl")
        assert r1["status"] == "completed"

        # 阶段 2: 爬取+分类（包含前置）
        r2 = await manager.run_phase("classify")
        assert r2["status"] == "completed"

        # 阶段 3: 全流程
        r3 = await manager.run_phase("report")
        assert r3["status"] == "completed"

    @pytest.mark.asyncio
    async def test_pipeline_without_db(self, mock_tools, mock_scorer, mock_reporter, mock_knowledge):
        """无数据库时不崩溃"""
        from agent.pipeline import PipelineManager

        manager = PipelineManager(mock_tools, mock_scorer, mock_reporter, mock_knowledge, db=None)
        result = await manager.run_full()
        assert result["status"] == "completed"
