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
    from auth.deps import get_current_user
    from main import app as _app

    async def override_current_user():
        return "local-user"

    _app.dependency_overrides[get_current_user] = override_current_user

    _app.state.db = db
    _app.state.knowledge_loader = knowledge_loader
    _app.state.draft_gen = draft_gen
    _app.state.draft_reviewer = None
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
    """Mock MongoDB — 支持 articles 和 chat_sessions 集合"""
    db = MagicMock()
    articles = MagicMock()
    chat_sessions = MagicMock()
    user_drafts = MagicMock()
    user_profiles = MagicMock()
    user_activities = MagicMock()
    pipeline_logs = MagicMock()

    # 默认返回带草稿的文章
    articles.find_one = AsyncMock(return_value=sample_article_with_drafts.copy())
    articles.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    user_drafts.find_one = AsyncMock(
        return_value={
            "user_id": "local-user",
            "article_url_hash": sample_article_with_drafts["url_hash"],
            "drafts": sample_article_with_drafts["pr_drafts"],
        }
    )
    user_drafts.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    user_profiles.find_one = AsyncMock(return_value=None)

    # chat_sessions 默认返回空（无历史）
    chat_sessions.find_one = AsyncMock(return_value=None)
    chat_sessions.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    chat_sessions.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
    user_activities.insert_one = AsyncMock(return_value=MagicMock(inserted_id="activity-id"))
    pipeline_logs.insert_one = AsyncMock(return_value=MagicMock(inserted_id="log-id"))

    def _get_collection(key):
        if key == "articles":
            return articles
        if key == "chat_sessions":
            return chat_sessions
        if key == "user_drafts":
            return user_drafts
        if key == "user_profiles":
            return user_profiles
        if key == "user_activities":
            return user_activities
        if key == "pipeline_logs":
            return pipeline_logs
        return MagicMock()

    db.__getitem__.side_effect = _get_collection
    db._articles = articles
    db._chat_sessions = chat_sessions
    db._user_drafts = user_drafts
    db._user_profiles = user_profiles
    db._user_activities = user_activities
    db._pipeline_logs = pipeline_logs
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
        log_document = app.state.db._pipeline_logs.insert_one.await_args.args[0]
        assert log_document["phase"] == "chat_ask"
        assert log_document["action"] == "complete"
        assert log_document["detail"]["question_length"] == 2
        assert "问题" not in str(log_document)

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


# ═══════════════════════════════════════════════════════════════
# 4. GET /api/articles/{url_hash}/drafts/{draft_index}/chat-history
# ═══════════════════════════════════════════════════════════════


class TestGetChatHistory:
    """获取对话历史端点测试"""

    @pytest.mark.asyncio
    async def test_get_history_empty(self, app):
        """无历史记录返回空数组"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/chat-history",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["messages"] == []

    @pytest.mark.asyncio
    async def test_get_history_with_messages(self, app, mock_db):
        """有历史记录返回消息列表"""
        mock_db._chat_sessions.find_one = AsyncMock(
            return_value={
                "article_url_hash": "d41d8cd98f00b204e9800998ecf8427e",
                "draft_index": 0,
                "messages": [
                    {"role": "user", "content": "问题1", "created_at": "2026-07-07T10:00:00"},
                    {"role": "assistant", "content": "回答1", "created_at": "2026-07-07T10:00:01"},
                ],
            }
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/chat-history",
            )

        assert resp.status_code == 200
        data = resp.json()
        messages = data["data"]["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "问题1"
        assert messages[1]["role"] == "assistant"
        mock_db._chat_sessions.find_one.assert_awaited_once_with(
            {
                "user_id": "local-user",
                "article_url_hash": "d41d8cd98f00b204e9800998ecf8427e",
                "draft_index": 0,
            }
        )


# ═══════════════════════════════════════════════════════════════
# 5. DELETE /api/articles/{url_hash}/drafts/{draft_index}/chat-history
# ═══════════════════════════════════════════════════════════════


class TestClearChatHistory:
    """清空对话历史端点测试"""

    @pytest.mark.asyncio
    async def test_clear_history_success(self, app, mock_db):
        """清空成功"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/chat-history",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["cleared"] is True
        mock_db._chat_sessions.delete_one.assert_awaited_once_with(
            {
                "user_id": "local-user",
                "article_url_hash": "d41d8cd98f00b204e9800998ecf8427e",
                "draft_index": 0,
            }
        )

    @pytest.mark.asyncio
    async def test_clear_history_no_existing_session(self, app, mock_db):
        """清空不存在的会话不报错"""
        mock_db._chat_sessions.delete_one = AsyncMock(
            return_value=MagicMock(deleted_count=0),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/chat-history",
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["cleared"] is True


# ═══════════════════════════════════════════════════════════════
# 6. 对话历史持久化验证
# ═══════════════════════════════════════════════════════════════


class TestChatHistoryPersistence:
    """验证 ask/revise 端点自动保存对话到 chat_sessions"""

    @pytest.mark.asyncio
    async def test_ask_saves_to_chat_sessions(self, app, mock_draft_gen, mock_db):
        """问答后自动保存到 chat_sessions"""
        from langchain_core.messages import AIMessage

        mock_draft_gen.llm.ainvoke = AsyncMock(return_value=AIMessage(content="回答"))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/chat/ask",
                json={
                    "message": "测试问题",
                    "article_url_hash": "d41d8cd98f00b204e9800998ecf8427e",
                    "draft_index": 0,
                },
            )

        assert resp.status_code == 200
        assert mock_db._chat_sessions.update_one.call_count >= 2
        query = mock_db._chat_sessions.update_one.await_args_list[0].args[0]
        assert query["user_id"] == "local-user"

    @pytest.mark.asyncio
    async def test_revise_saves_to_chat_sessions(self, app, mock_draft_gen, mock_db):
        """改稿后自动保存到 chat_sessions"""
        from langchain_core.messages import AIMessage

        mock_draft_gen.llm.ainvoke = AsyncMock(
            return_value=AIMessage(content="## 修改摘要\n- 修改1\n\n## 修订稿\n# [新标题]\n正文"),
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/revise",
                json={"instruction": "改意见", "save": False},
            )

        assert resp.status_code == 200
        assert mock_db._chat_sessions.update_one.call_count >= 2

    @pytest.mark.asyncio
    async def test_ask_without_article_does_not_save(self, app, mock_draft_gen, mock_db):
        """无 article_url_hash 时不保存对话"""
        from langchain_core.messages import AIMessage

        mock_draft_gen.llm.ainvoke = AsyncMock(return_value=AIMessage(content="回答"))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/chat/ask", json={"message": "问题"})

        assert resp.status_code == 200
        mock_db._chat_sessions.update_one.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 7. TestChatAsk 续（草稿不存在）
# ═══════════════════════════════════════════════════════════════


class TestChatAskDraftNotFound:
    """草稿不存在测试（从 TestChatAsk 分离）"""

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
        mock_db["user_drafts"].update_one.assert_not_called()

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
        mock_db["user_drafts"].update_one.assert_called_once()
        call_args = mock_db["user_drafts"].update_one.call_args
        assert call_args[0][0]["article_url_hash"] == "d41d8cd98f00b204e9800998ecf8427e"
        activity = mock_db._user_activities.insert_one.await_args.args[0]
        assert activity["action"] == "draft_revise"
        assert activity["target"]["article_url_hash"] == "d41d8cd98f00b204e9800998ecf8427e"

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
        mock_db["user_drafts"].find_one = AsyncMock(return_value={"drafts": article["pr_drafts"]})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/revisions/rev-001/apply",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["data"]["applied"] is True
        assert data["data"]["revision_id"] == "rev-001"
        activity = mock_db._user_activities.insert_one.await_args.args[0]
        assert activity["action"] == "revision_apply"
        assert activity["target"]["revision_id"] == "rev-001"
        log_document = mock_db._pipeline_logs.insert_one.await_args.args[0]
        assert log_document["phase"] == "chat_apply"
        assert log_document["detail"]["revision_id"] == "rev-001"
        # 验证写入 DB
        mock_db["user_drafts"].update_one.assert_called_once()

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
        mock_db["user_drafts"].find_one = AsyncMock(return_value={"drafts": article["pr_drafts"]})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/articles/d41d8cd98f00b204e9800998ecf8427e/drafts/0/revisions/nonexistent/apply",
            )

        assert resp.status_code == 404
