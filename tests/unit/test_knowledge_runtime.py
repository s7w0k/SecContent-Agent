"""
知识库运行时热刷新 - 单元测试

运行:
    pytest tests/unit/test_knowledge_runtime.py -v
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.wiki.provider import LegacyKnowledgeProvider

# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def mock_loader():
    """模拟 KnowledgeLoader。"""
    loader = MagicMock()
    loader._last_hash = "abcdef1234567890"
    loader.reload_if_changed = AsyncMock(return_value=False)
    return loader


@pytest.fixture
def mock_scorer_v2():
    """模拟 ScoringAgentV2。"""
    scorer = MagicMock()
    scorer.refresh_prompt = MagicMock()
    return scorer


@pytest.fixture
def app_state(mock_loader, mock_scorer_v2):
    """模拟 app.state。"""
    return SimpleNamespace(
        knowledge_loader=mock_loader,
        scorer_v2=mock_scorer_v2,
        db=None,
    )


@pytest.fixture
def app_state_no_loader(mock_scorer_v2):
    """模拟无 knowledge_loader 的 app.state。"""
    return SimpleNamespace(
        knowledge_loader=None,
        scorer_v2=mock_scorer_v2,
        db=None,
    )


# ═══════════════════════════════════════════════════════════════
# KnowledgeRuntimeRefresher 测试
# ═══════════════════════════════════════════════════════════════


class TestRefreshIfChanged:
    """refresh_if_changed 方法测试。"""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_loader(self, app_state_no_loader):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        refresher = KnowledgeRuntimeRefresher(app_state_no_loader)
        result = await refresher.refresh_if_changed()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_change(self, app_state, mock_loader):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        mock_loader.reload_if_changed = AsyncMock(return_value=False)

        refresher = KnowledgeRuntimeRefresher(app_state)
        result = await refresher.refresh_if_changed()
        assert result is False
        mock_loader.reload_if_changed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_true_and_refreshes_when_changed(
        self, app_state, mock_loader, mock_scorer_v2
    ):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        mock_loader.reload_if_changed = AsyncMock(return_value=True)

        refresher = KnowledgeRuntimeRefresher(app_state)
        result = await refresher.refresh_if_changed()
        assert result is True
        mock_loader.reload_if_changed.assert_awaited_once()
        mock_scorer_v2.refresh_prompt.assert_called_once()


class TestRefreshAgents:
    """_refresh_agents 方法测试。"""

    @pytest.mark.asyncio
    async def test_calls_scorer_v2_refresh_prompt(self, app_state, mock_scorer_v2):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        refresher = KnowledgeRuntimeRefresher(app_state)
        refresher._refresh_agents()
        mock_scorer_v2.refresh_prompt.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_error_when_scorer_v2_missing(self, mock_loader):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        state = SimpleNamespace(
            knowledge_loader=mock_loader,
            scorer_v2=None,
            db=None,
        )
        refresher = KnowledgeRuntimeRefresher(state)
        # Should not raise
        refresher._refresh_agents()


class TestGetCurrentHash:
    """get_current_hash 方法测试。"""

    @pytest.mark.asyncio
    async def test_returns_hash_when_loader_exists(self, app_state):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        refresher = KnowledgeRuntimeRefresher(app_state)
        result = await refresher.get_current_hash()
        assert result == "abcdef1234567890"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_loader(self, app_state_no_loader):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        refresher = KnowledgeRuntimeRefresher(app_state_no_loader)
        result = await refresher.get_current_hash()
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_hash_is_none(self, mock_scorer_v2):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        loader = MagicMock()
        loader._last_hash = None
        state = SimpleNamespace(
            knowledge_loader=loader,
            scorer_v2=mock_scorer_v2,
            db=None,
        )
        refresher = KnowledgeRuntimeRefresher(state)
        result = await refresher.get_current_hash()
        assert result == ""


class TestPrepareForTask:
    """prepare_for_task 方法测试。"""

    @pytest.mark.asyncio
    async def test_returns_hash_when_no_lock(self, app_state, mock_loader):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        refresher = KnowledgeRuntimeRefresher(app_state)
        result = await refresher.prepare_for_task()
        assert result == "abcdef1234567890"
        mock_loader.reload_if_changed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_waits_for_lock_then_proceeds(self, mock_loader, mock_scorer_v2):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        # Mock DB with lock that expires
        now = datetime.now(UTC)
        lock_doc = {
            "lock_key": "global-knowledge-publication",
            "expires_at": now - timedelta(seconds=1),  # Already expired
        }

        locks_collection = MagicMock()
        locks_collection.find_one = AsyncMock(return_value=lock_doc)
        db = MagicMock()
        db.__getitem__.return_value = locks_collection

        state = SimpleNamespace(
            knowledge_loader=mock_loader,
            scorer_v2=mock_scorer_v2,
            db=db,
        )

        refresher = KnowledgeRuntimeRefresher(state)
        result = await refresher.prepare_for_task()
        assert result == "abcdef1234567890"

    @pytest.mark.asyncio
    async def test_no_db_skips_lock_check(self, app_state, mock_loader):
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        refresher = KnowledgeRuntimeRefresher(app_state)
        result = await refresher.prepare_for_task()
        assert result == "abcdef1234567890"


# ═══════════════════════════════════════════════════════════════
# ScoringAgentV2.refresh_prompt 测试
# ═══════════════════════════════════════════════════════════════


class TestScoringAgentV2RefreshPrompt:
    """ScoringAgentV2.refresh_prompt 方法测试。"""

    def test_refresh_prompt_rebuilds_system_prompt(self):
        from agent.knowledge import ProductKnowledge
        from agent.scorer_v2 import ScoringAgentV2

        knowledge = ProductKnowledge(
            product_name="测试产品",
            product_positioning="测试定位",
        )

        llm = MagicMock()
        scorer = ScoringAgentV2(
            llm=llm, knowledge=knowledge, db=None, knowledge_provider=LegacyKnowledgeProvider()
        )

        original_prompt = scorer.system_prompt
        assert "测试产品" in original_prompt

        # Modify knowledge and refresh
        knowledge.product_name = "更新后的产品"
        scorer.refresh_prompt()

        refreshed_prompt = scorer.system_prompt
        assert "更新后的产品" in refreshed_prompt
        assert refreshed_prompt != original_prompt

    def test_refresh_prompt_uses_scoring_prompt_when_available(self):
        """当 knowledge 有 as_scoring_prompt 方法时，refresh_prompt 应使用它。"""
        from agent.scorer_v2 import ScoringAgentV2

        knowledge = MagicMock()
        knowledge.as_scoring_prompt.return_value = "自定义评分Prompt内容"

        llm = MagicMock()
        scorer = ScoringAgentV2(
            llm=llm, knowledge=knowledge, db=None, knowledge_provider=LegacyKnowledgeProvider()
        )

        original_prompt = scorer.system_prompt
        assert "自定义评分Prompt内容" in original_prompt

        # Change and refresh
        knowledge.as_scoring_prompt.return_value = "更新后的评分Prompt"
        scorer.refresh_prompt()

        assert "更新后的评分Prompt" in scorer.system_prompt
