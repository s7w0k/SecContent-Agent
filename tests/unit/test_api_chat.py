"""
对话改稿 REST API — 单元测试

运行:
    pytest tests/unit/test_api_chat.py -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _make_app(db=None, knowledge_loader=None, draft_gen=None):
    """创建测试用 FastAPI app，注入 mock 依赖"""
    from main import app as _app

    _app.state.db = db
    _app.state.knowledge_loader = knowledge_loader
    _app.state.draft_gen = draft_gen
    # 清除缓存的 agent，确保每次测试独立
    if hasattr(_app.state, "draft_chat_agent"):
        delattr(_app.state, "draft_chat_agent")
    return _app


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def mock_knowledge():
    """Mock KnowledgeLoader"""
    loader = MagicMock()

    class FakeCache:
        product_name = "测试产品"

        def as_system_prompt(self):
            return "## 产品\n测试产品"

    loader._cache = FakeCache()
    return loader


@pytest.fixture
def mock_draft_gen():
    """Mock DraftGenerator（提供 .llm 属性）"""
    gen = MagicMock()
    gen.llm = MagicMock()
    gen.llm.temperature = None
    return gen


@pytest.fixture
def sample_article_with_drafts():
    """带 pr_drafts 的文章"""
    return {
        "_id": "507f1f77bcf86cd799439011",
        "url_hash": "d41d8cd98f00b204e9800998ecf8427e",
        "title": "Critical MCP Vulnerability",
        "source": "The Hacker News",
        "category_v2": "爆点事件",
        "product_relevance": 85,
        "event_impact": 72,
        "pr_total_score": 157,
        "summary_cn": "MCP服务器中发现严重漏洞",
        "content_md": "# 原文\n\n文章正文",
        "pr_drafts": [
            {
                "template": "爆点A",
                "perspective": "产品能力视角",
                "content_md": "# [原标题]\n\n## 导语\n原始草稿",
                "title": "Critical MCP Vulnerability",
                "index": 1,
            },
        ],
    }


@pytest.fixture
def mock_db(sample_article_with_drafts):
    """Mock MongoDB"""
    db = MagicMock()
    articles = MagicMock()

    # 默认返回带草稿的文章
    articles.find_one = AsyncMock(return_value=sample_article_with_drafts.copy())
    articles.update_one = AsyncMock(return_value=MagicMock(modified_count=1))

    db.__getitem__.side_effect = lambda key: articles if key == "articles" else MagicMock()
    return db


@pytest.fixture
def app(mock_db, mock_knowledge, mock_draft_gen):
    return _make_app(db=mock_db, knowledge_loader=mock_knowledge, draft_gen=mock_draft_gen)


@pytest.fixture
def app_no_draft_gen(mock_db, mock_knowledge):
    """无 draft_gen 的 app（测试 503 场景）"""
    return _make_app(db=mock_db, knowledge_loader=mock_knowledge, draft_gen=None)


# ═══════════════════════════════════════════════════════════════
# 1. POST /api/chat/ask
# ═══════════════════════════════════════════════════════════════


class TestChatAsk:
    """问答端点测试"""

    @pytest.mark.asyncio
    async def test_ask_success(self, app, mock_draft_gen):
        """问答成功"""
        from langchain_core.messages import AIMessage

        mock_draft_gen.llm.ainvoke = AsyncMock(return_value=AIMessage(content="这是回答。"))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/chat/ask", json={"message": "问题"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["answer"] == "这是回答。"
        assert "knowledge" in data["data"]["references"]

    @pytest.mark.asyncio
    async def test_ask_with_article_and_draft(self, app, mock_draft_gen, mock_db):
        """带文章和草稿的问答"""
        from langchain_core.messages import AIMessage

        mock_draft_gen.llm.ainvoke = AsyncMock(return_value=AIMessage(content="基于文章的回答。"))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/chat/ask",
                json={
                    "message": "传播角度够强吗？",
                    "article_url_hash": "d41d8cd98f00b204e9800998ecf8427e",
                    "draft_index": 0,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "article" in data["data"]["references"]
        assert "draft" in data["data"]["references"]

    @pytest.mark.asyncio
    async def test_ask_empty_message_returns_422(self, app):
        """空 message 返回 422"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/chat/ask", json={"message": ""})

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_ask_article_not_found_returns_404(self, app, mock_db):
        """文章不存在返回 404"""
        mock_db["articles"].find_one = AsyncMock(return_value=None)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/chat/ask",
                json={
                    "message": "问题",
                    "article_url_hash": "nonexistenthash12345678901234567890",
                },
            )

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_ask_draft_not_found_returns_404(self, app, mock_db, sample_article_with_drafts):
        """草稿不存在返回 404"""
        mock_db["articles"].find_one = AsyncMock(return_value=sample_article_with_drafts.copy())

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/chat/ask",
                json={
                    "message": "问题",
                    "article_url_hash": "d41d8cd98f00b204e9800998ecf8427e",
                    "draft_index": 99,
                },
            )

        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════
# 2. POST /api/articles/{url_hash}/drafts/{draft_index}/revise
# ═══════════════════════════════════════════════════════════════


class TestReviseDraft:
    """改稿端点测试"""

    @pytest.mark.asyncio
    async def test_revise_save_false(self, app, mock_draft_gen, mock_db):
        """save=false 不写 DB"""
        from langchain_core.messages import AIMessage

        mock_draft_gen.llm.ainvoke = AsyncMock(
            return_value=AIMessage(content="## 修改摘要\n- 修改1\n\n## 修订稿\n# [新标题]\n正文")
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/revise",
                json={"instruction": "改得更通俗", "save": False},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["saved"] is False
        assert data["data"]["revised_content_md"].startswith("# [新标题]")
        assert data["data"]["change_summary"] == ["修改1"]
        # 验证未写入 DB
        mock_db["articles"].update_one.assert_not_called()

    @pytest.mark.asyncio
    async def test_revise_save_true(self, app, mock_draft_gen, mock_db):
        """save=true 写入 revisions"""
        from langchain_core.messages import AIMessage

        mock_draft_gen.llm.ainvoke = AsyncMock(
            return_value=AIMessage(content="## 修改摘要\n- 修改1\n\n## 修订稿\n# [新标题]\n正文")
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/revise",
                json={"instruction": "改得更通俗", "save": True},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["saved"] is True
        assert data["data"]["revision_id"]  # uuid 已生成
        # 验证写入 DB
        mock_db["articles"].update_one.assert_called_once()
        call_args = mock_db["articles"].update_one.call_args
        assert call_args[0][0]["url_hash"] == "d41d8cd98f00b204e9800998ecf8427e"

    @pytest.mark.asyncio
    async def test_revise_article_not_found(self, app, mock_db):
        """文章不存在返回 404"""
        mock_db["articles"].find_one = AsyncMock(return_value=None)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/nonexistent/drafts/0/revise",
                json={"instruction": "改意见"},
            )

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_revise_draft_index_out_of_range(self, app, mock_db, sample_article_with_drafts):
        """草稿序号越界返回 404"""
        mock_db["articles"].find_one = AsyncMock(return_value=sample_article_with_drafts.copy())

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/99/revise",
                json={"instruction": "改意见"},
            )

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_revise_empty_instruction_returns_422(self, app):
        """空 instruction 返回 422"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/revise",
                json={"instruction": ""},
            )

        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════
# 3. POST /api/articles/{url_hash}/drafts/{draft_index}/revisions/{revision_id}/apply
# ═══════════════════════════════════════════════════════════════


class TestApplyRevision:
    """应用修订端点测试"""

    @pytest.mark.asyncio
    async def test_apply_success(self, app, mock_db):
        """应用修订成功"""
        article = {
            "url_hash": "d41d8cd98f00b204e9800998ecf8427e",
            "pr_drafts": [
                {
                    "template": "爆点A",
                    "content_md": "原始内容",
                    "revisions": [
                        {
                            "revision_id": "rev-001",
                            "content_md": "修订后内容",
                            "applied": False,
                        },
                    ],
                },
            ],
        }
        mock_db["articles"].find_one = AsyncMock(return_value=article)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/revisions/rev-001/apply",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["applied"] is True
        assert data["data"]["revision_id"] == "rev-001"
        # 验证写入 DB
        mock_db["articles"].update_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_article_not_found(self, app, mock_db):
        """文章不存在返回 404"""
        mock_db["articles"].find_one = AsyncMock(return_value=None)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/nonexistent/drafts/0/revisions/rev-001/apply",
            )

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_apply_revision_not_found(self, app, mock_db):
        """修订记录不存在返回 404"""
        article = {
            "url_hash": "d41d8cd98f00b204e9800998ecf8427e",
            "pr_drafts": [
                {
                    "content_md": "原始内容",
                    "revisions": [],
                },
            ],
        }
        mock_db["articles"].find_one = AsyncMock(return_value=article)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/revisions/nonexistent/apply",
            )

        assert resp.status_code == 404
