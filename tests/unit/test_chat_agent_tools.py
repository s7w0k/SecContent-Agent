"""Agent 工具集安全测试 -- 阶段一 Step 4。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.agent_contracts import RunContext
from agent.agent_tools import TOOL_POLICIES, create_agent_tools


def _make_ctx(**kwargs) -> RunContext:
    defaults = {"trace_id": "t1", "run_id": "r1", "user_id": "u1"}
    defaults.update(kwargs)
    return RunContext(**defaults)


def _make_db():
    db = MagicMock()
    return db


class TestToolPolicies:
    def test_all_three_policies_exist(self):
        assert "search_knowledge" in TOOL_POLICIES
        assert "retrieve_memory" in TOOL_POLICIES
        assert "get_article" in TOOL_POLICIES

    def test_all_idempotent(self):
        for p in TOOL_POLICIES.values():
            assert p.idempotent is True

    def test_search_knowledge_requires_product_allowlist(self):
        assert TOOL_POLICIES["search_knowledge"].requires_product_allowlist

    def test_get_article_requires_article_allowlist(self):
        assert TOOL_POLICIES["get_article"].requires_article_allowlist


class TestSearchKnowledge:
    @pytest.mark.asyncio
    async def test_blocked_unauthorized_product(self):
        ctx = _make_ctx(allowed_product_ids=frozenset({"p1"}))
        db = _make_db()
        tools = create_agent_tools(db=db, run_context=ctx)
        search = tools[0]

        result = await search.ainvoke({"product_id": "p_hacker", "purpose": "chat"})
        assert "不在允许列表内" in result
        assert "permission_denied" in result

    @pytest.mark.asyncio
    async def test_blocked_empty_allowlist(self):
        ctx = _make_ctx()  # no allowed_product_ids
        db = _make_db()
        tools = create_agent_tools(db=db, run_context=ctx)
        search = tools[0]

        result = await search.ainvoke({"product_id": "any", "purpose": "chat"})
        assert "不在允许列表内" in result

    @pytest.mark.asyncio
    async def test_success(self):
        ctx = _make_ctx(allowed_product_ids=frozenset({"p1"}))
        db = _make_db()
        # mock KnowledgeSliceResolver
        mock_slice = MagicMock()
        mock_slice.content = "产品定位: 智能体安全"
        mock_slice.source_document_ids = ["doc1.md", "doc2.md"]
        mock_slice.truncated = False

        with pytest.MonkeyPatch.context() as m:
            mock_resolver = MagicMock()
            mock_resolver.resolve = AsyncMock(return_value=mock_slice)
            m.setattr(
                "agent.knowledge_slice.KnowledgeSliceResolver",
                lambda **kw: mock_resolver,
            )
            tools = create_agent_tools(db=db, run_context=ctx)
            search = tools[0]
            result = await search.ainvoke({"product_id": "p1", "purpose": "chat"})

        assert "产品定位" in result
        assert "permission_denied" not in result

    @pytest.mark.asyncio
    async def test_empty_knowledge(self):
        ctx = _make_ctx(allowed_product_ids=frozenset({"p1"}))
        db = _make_db()
        mock_slice = MagicMock()
        mock_slice.content = ""
        mock_slice.source_document_ids = []

        with pytest.MonkeyPatch.context() as m:
            mock_resolver = MagicMock()
            mock_resolver.resolve = AsyncMock(return_value=mock_slice)
            m.setattr(
                "agent.knowledge_slice.KnowledgeSliceResolver",
                lambda **kw: mock_resolver,
            )
            tools = create_agent_tools(db=db, run_context=ctx)
            search = tools[0]
            result = await search.ainvoke({"product_id": "p1"})

        assert "未找到" in result or "not_found" in result

    @pytest.mark.asyncio
    async def test_db_exception_handled(self):
        ctx = _make_ctx(allowed_product_ids=frozenset({"p1"}))
        db = _make_db()

        with pytest.MonkeyPatch.context() as m:
            mock_resolver = MagicMock()
            mock_resolver.resolve = AsyncMock(side_effect=Exception("DB down"))
            m.setattr(
                "agent.knowledge_slice.KnowledgeSliceResolver",
                lambda **kw: mock_resolver,
            )
            tools = create_agent_tools(db=db, run_context=ctx)
            search = tools[0]
            result = await search.ainvoke({"product_id": "p1"})

        assert "失败" in result or "db_error" in result


class TestRetrieveMemory:
    @pytest.mark.asyncio
    async def test_feature_disabled(self):
        ctx = _make_ctx()
        db = _make_db()

        with pytest.MonkeyPatch.context() as m:
            mock_settings = MagicMock()
            mock_settings.MEMORY_FEATURE_ENABLED = False
            m.setattr("config.get_settings", lambda: mock_settings)
            tools = create_agent_tools(db=db, run_context=ctx)
            memory_tool = tools[1]
            result = await memory_tool.ainvoke({})

        assert "未启用" in result or "feature_disabled" in result

    @pytest.mark.asyncio
    async def test_empty_memory(self):
        ctx = _make_ctx()
        db = _make_db()
        mock_pack = MagicMock()
        mock_pack.rendered_text = ""
        mock_pack.memory_ids = []

        with pytest.MonkeyPatch.context() as m:
            mock_settings = MagicMock()
            mock_settings.MEMORY_FEATURE_ENABLED = True
            m.setattr("config.get_settings", lambda: mock_settings)

            mock_retriever = MagicMock()
            mock_retriever.retrieve = AsyncMock(return_value=mock_pack)
            m.setattr(
                "agent.memory_retriever.MemoryRetriever",
                lambda db: mock_retriever,
            )
            tools = create_agent_tools(db=db, run_context=ctx)
            memory_tool = tools[1]
            result = await memory_tool.ainvoke({})

        assert "暂无" in result or "empty" in result

    @pytest.mark.asyncio
    async def test_success(self):
        ctx = _make_ctx()
        db = _make_db()
        mock_pack = MagicMock()
        mock_pack.rendered_text = "用户偏好: 简洁风格"
        mock_pack.memory_ids = ["m1", "m2"]

        with pytest.MonkeyPatch.context() as m:
            mock_settings = MagicMock()
            mock_settings.MEMORY_FEATURE_ENABLED = True
            m.setattr("config.get_settings", lambda: mock_settings)

            mock_retriever = MagicMock()
            mock_retriever.retrieve = AsyncMock(return_value=mock_pack)
            m.setattr(
                "agent.memory_retriever.MemoryRetriever",
                lambda db: mock_retriever,
            )
            tools = create_agent_tools(db=db, run_context=ctx)
            memory_tool = tools[1]
            result = await memory_tool.ainvoke({})

        assert "简洁风格" in result


class TestGetArticle:
    @pytest.mark.asyncio
    async def test_blocked_unauthorized_article(self):
        ctx = _make_ctx(allowed_article_hashes=frozenset({"hash_a"}))
        db = _make_db()
        tools = create_agent_tools(db=db, run_context=ctx)
        get_article = tools[2]

        result = await get_article.ainvoke({"url_hash": "hash_hacker"})
        assert "不在允许列表内" in result
        assert "permission_denied" in result

    @pytest.mark.asyncio
    async def test_article_not_found(self):
        ctx = _make_ctx(allowed_article_hashes=frozenset({"hash_a"}))
        db = _make_db()
        db["articles"] = MagicMock()
        db["articles"].find_one = AsyncMock(return_value=None)

        tools = create_agent_tools(db=db, run_context=ctx)
        get_article = tools[2]
        result = await get_article.ainvoke({"url_hash": "hash_a"})

        assert "不存在" in result or "not_found" in result

    @pytest.mark.asyncio
    async def test_success(self):
        ctx = _make_ctx(allowed_article_hashes=frozenset({"hash_a"}))
        db = _make_db()
        db["articles"] = MagicMock()
        db["articles"].find_one = AsyncMock(
            return_value={
                "title": "测试文章",
                "source": "Help Net Security",
                "category_v2": "ai_progress",
                "summary": "AI security news",
                "content_md": "正文内容" * 100,
            }
        )

        tools = create_agent_tools(db=db, run_context=ctx)
        get_article = tools[2]
        result = await get_article.ainvoke({"url_hash": "hash_a"})

        assert "测试文章" in result
        assert "Help Net Security" in result
        assert "ai_progress" in result

    @pytest.mark.asyncio
    async def test_no_internal_fields_exposed(self):
        ctx = _make_ctx(allowed_article_hashes=frozenset({"hash_a"}))
        db = _make_db()
        db["articles"] = MagicMock()
        db["articles"].find_one = AsyncMock(
            return_value={
                "title": "测试",
                "source": "src",
                "content_md": "正文",
                "_id": "secret_object_id",
                "user_id": "secret_user",
            }
        )

        tools = create_agent_tools(db=db, run_context=ctx)
        get_article = tools[2]
        result = await get_article.ainvoke({"url_hash": "hash_a"})

        assert "secret_object_id" not in result
        assert "secret_user" not in result

    @pytest.mark.asyncio
    async def test_db_exception_handled(self):
        ctx = _make_ctx(allowed_article_hashes=frozenset({"hash_a"}))
        db = _make_db()
        db["articles"] = MagicMock()
        db["articles"].find_one = AsyncMock(side_effect=Exception("DB down"))

        tools = create_agent_tools(db=db, run_context=ctx)
        get_article = tools[2]
        result = await get_article.ainvoke({"url_hash": "hash_a"})

        assert "失败" in result or "db_error" in result

    @pytest.mark.asyncio
    async def test_truncation(self):
        """超长正文被截断。"""
        ctx = _make_ctx(allowed_article_hashes=frozenset({"hash_a"}))
        db = _make_db()
        db["articles"] = MagicMock()
        long_content = "A" * 5000
        db["articles"].find_one = AsyncMock(
            return_value={
                "title": "长文",
                "content_md": long_content,
            }
        )

        tools = create_agent_tools(db=db, run_context=ctx)
        get_article = tools[2]
        result = await get_article.ainvoke({"url_hash": "hash_a"})

        assert "truncated" in result or len(result) < 5000
