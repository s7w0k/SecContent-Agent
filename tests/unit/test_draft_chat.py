"""
DraftChatAgent — 单元测试

运行:
    pytest tests/unit/test_draft_chat.py -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.draft_chat import (
    DraftChatAgent,
    LLMError,
    clean_markdown,
    parse_revise_output,
)

# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def knowledge_loader():
    """Mock KnowledgeLoader"""
    loader = MagicMock()

    class FakeCache:
        product_name = "智能体身份安全产品"
        product_positioning = "定义AI时代的安全身份通行证"
        core_features = ["MCP协议安全防护", "智能体身份认证"]
        tech_barriers = ["动态上下文感知"]
        control_points = ["首家MCP安全审计"]
        customer_cases = ["北京移动"]
        competitors = ["竞品A"]
        target_industries = ["运营商"]
        key_terms = ["MCP", "Agent安全"]
        source_files = ["产品知识库.md"]

        def as_system_prompt(self):
            return "## 产品\n智能体身份安全产品\n## 核心功能\n- MCP协议安全防护"

    loader._cache = FakeCache()
    return loader


@pytest.fixture
def mock_llm_answer():
    """Mock LLM 返回问答结果"""
    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(return_value=AIMessage(content="这是一个测试回答。"))
    return llm


@pytest.fixture
def mock_llm_revise():
    """Mock LLM 返回改稿结果"""
    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content="## 修改摘要\n- 增强标题传播性\n- 压缩技术细节\n\n## 修订稿\n# [新标题]\n\n改写后的内容。"
        )
    )
    return llm


@pytest.fixture
def sample_article():
    """测试用文章数据"""
    return {
        "title": "Critical MCP Vulnerability",
        "source": "The Hacker News",
        "category_v2": "爆点事件",
        "product_relevance": 85,
        "event_impact": 72,
        "pr_total_score": 157,
        "summary_cn": "MCP服务器中发现严重漏洞",
        "content_md": "# 原文\n\n文章正文内容...",
    }


@pytest.fixture
def sample_draft():
    """测试用草稿数据"""
    return {
        "template": "爆点A",
        "perspective": "产品能力视角",
        "content_md": "# [原标题]\n\n## 导语\n原始草稿内容",
        "title": "Critical MCP Vulnerability",
        "index": 1,
    }


@pytest.fixture
def agent(mock_llm_answer, knowledge_loader):
    return DraftChatAgent(llm=mock_llm_answer, knowledge_loader=knowledge_loader)


# ═══════════════════════════════════════════════════════════════
# 1. 问答 Prompt 构造测试
# ═══════════════════════════════════════════════════════════════


class TestAnswerPrompt:
    """answer() 方法的 Prompt 构造"""

    @pytest.mark.asyncio
    async def test_answer_returns_answer_field(self, agent):
        """answer() 返回包含 answer 字段"""
        result = await agent.answer(message="这篇稿子传播角度够强吗？")
        assert "answer" in result
        assert result["answer"] == "这是一个测试回答。"

    @pytest.mark.asyncio
    async def test_answer_returns_references(self, agent, sample_article, sample_draft):
        """answer() 带文章和草稿时返回正确 references"""
        result = await agent.answer(
            message="问题",
            article=sample_article,
            draft=sample_draft,
        )
        assert "references" in result
        assert "article" in result["references"]
        assert "draft" in result["references"]
        assert "knowledge" in result["references"]

    @pytest.mark.asyncio
    async def test_answer_references_without_context(self, agent):
        """answer() 无上下文时 references 只含 knowledge"""
        result = await agent.answer(message="通用问题")
        assert result["references"] == ["knowledge"]

    @pytest.mark.asyncio
    async def test_answer_llm_called_with_system_and_human(self, agent, mock_llm_answer):
        """验证 LLM 被调用，且传入 SystemMessage + HumanMessage"""
        await agent.answer(message="测试问题")
        mock_llm_answer.ainvoke.assert_called_once()
        messages = mock_llm_answer.ainvoke.call_args[0][0]
        assert len(messages) == 2  # SystemMessage + HumanMessage

    @pytest.mark.asyncio
    async def test_answer_with_history(self, agent, mock_llm_answer):
        """answer() 支持对话历史"""
        history = [
            {"role": "user", "content": "上一个问题"},
            {"role": "assistant", "content": "上一个回答"},
        ]
        await agent.answer(message="新问题", history=history)
        mock_llm_answer.ainvoke.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# 2. 改稿 Prompt 构造测试
# ═══════════════════════════════════════════════════════════════


class TestRevisePrompt:
    """revise() 方法的 Prompt 构造"""

    @pytest.mark.asyncio
    async def test_revise_returns_content_and_summary(
        self, mock_llm_revise, knowledge_loader, sample_article, sample_draft
    ):
        """revise() 返回 revised_content_md 和 change_summary"""
        agent = DraftChatAgent(llm=mock_llm_revise, knowledge_loader=knowledge_loader)
        result = await agent.revise(
            instruction="标题更有冲击力",
            article=sample_article,
            draft=sample_draft,
        )
        assert "revised_content_md" in result
        assert "change_summary" in result
        assert len(result["change_summary"]) == 2
        assert "增强标题传播性" in result["change_summary"]

    @pytest.mark.asyncio
    async def test_revise_llm_called(
        self, mock_llm_revise, knowledge_loader, sample_article, sample_draft
    ):
        """验证 LLM 被调用"""
        agent = DraftChatAgent(llm=mock_llm_revise, knowledge_loader=knowledge_loader)
        await agent.revise(instruction="改意见", article=sample_article, draft=sample_draft)
        mock_llm_revise.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_revise_prompt_contains_instruction(
        self, mock_llm_revise, knowledge_loader, sample_article, sample_draft
    ):
        """验证 Human Prompt 包含用户修改意见"""
        agent = DraftChatAgent(llm=mock_llm_revise, knowledge_loader=knowledge_loader)
        await agent.revise(instruction="减少技术细节", article=sample_article, draft=sample_draft)
        messages = mock_llm_revise.ainvoke.call_args[0][0]
        human_msg = messages[1]
        assert "减少技术细节" in human_msg.content


# ═══════════════════════════════════════════════════════════════
# 3. Markdown 清洗测试
# ═══════════════════════════════════════════════════════════════


class TestCleanMarkdown:
    """clean_markdown() 函数"""

    def test_removes_markdown_code_fence(self):
        """去除 ```markdown 包裹"""
        raw = "```markdown\n# 标题\n内容\n```"
        result = clean_markdown(raw)
        assert result == "# 标题\n内容"

    def test_removes_plain_code_fence(self):
        """去除纯 ``` 包裹"""
        raw = "```\n# 标题\n内容\n```"
        result = clean_markdown(raw)
        assert result == "# 标题\n内容"

    def test_no_code_fence_unchanged(self):
        """无代码块包裹时不变"""
        raw = "# 直接标题\n内容"
        result = clean_markdown(raw)
        assert result == raw

    def test_empty_string(self):
        """空字符串处理"""
        assert clean_markdown("") == ""

    def test_only_code_fence_markers(self):
        """只有代码块标记"""
        result = clean_markdown("```\n```")
        assert result == ""


# ═══════════════════════════════════════════════════════════════
# 4. 修改摘要解析测试
# ═══════════════════════════════════════════════════════════════


class TestParseReviseOutput:
    """parse_revise_output() 函数"""

    def test_normal_structure_with_dash(self):
        """正常结构 — 破折号列表"""
        output = "## 修改摘要\n- 第一项\n- 第二项\n\n## 修订稿\n# [标题]\n正文"
        summary, content = parse_revise_output(output)
        assert summary == ["第一项", "第二项"]
        assert content.startswith("# [标题]")

    def test_normal_structure_with_asterisk(self):
        """正常结构 — 星号列表"""
        output = "## 修改摘要\n* 第一项\n* 第二项\n\n## 修订稿\n# 标题\n正文"
        summary, _ = parse_revise_output(output)
        assert summary == ["第一项", "第二项"]

    def test_normal_structure_with_numbers(self):
        """正常结构 — 数字列表"""
        output = "## 修改摘要\n1. 第一项\n2. 第二项\n\n## 修订稿\n# 标题\n正文"
        summary, _ = parse_revise_output(output)
        assert summary == ["第一项", "第二项"]

    def test_fallback_no_structure(self):
        """解析失败 — 保底返回完整内容 + 空摘要"""
        output = "只有内容没有结构标记"
        summary, content = parse_revise_output(output)
        assert summary == []
        assert content == "只有内容没有结构标记"

    def test_fallback_only_summary_section(self):
        """只有修改摘要段，无修订稿段"""
        output = "## 修改摘要\n- 第一项\n\n没有修订稿部分"
        summary, content = parse_revise_output(output)
        assert summary == []
        assert "修改摘要" in content

    def test_code_fence_wrapped(self):
        """代码块包裹的输出"""
        output = "```markdown\n## 修改摘要\n- 第一项\n\n## 修订稿\n# 标题\n正文\n```"
        summary, content = parse_revise_output(output)
        assert summary == ["第一项"]
        assert content.startswith("# 标题")


# ═══════════════════════════════════════════════════════════════
# 5. LLM 异常处理测试
# ═══════════════════════════════════════════════════════════════


class TestLLMErrorHandling:
    """LLM 异常处理"""

    @pytest.mark.asyncio
    async def test_answer_raises_llm_error(self, knowledge_loader):
        """answer() LLM 失败时抛出 LLMError"""
        llm = MagicMock()
        llm.temperature = None
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("API timeout"))
        agent = DraftChatAgent(llm=llm, knowledge_loader=knowledge_loader)

        with pytest.raises(LLMError, match="LLM 调用失败"):
            await agent.answer(message="问题")

    @pytest.mark.asyncio
    async def test_revise_raises_llm_error(self, knowledge_loader, sample_article, sample_draft):
        """revise() LLM 失败时抛出 LLMError"""
        llm = MagicMock()
        llm.temperature = None
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("API timeout"))
        agent = DraftChatAgent(llm=llm, knowledge_loader=knowledge_loader)

        with pytest.raises(LLMError, match="LLM 调用失败"):
            await agent.revise(instruction="意见", article=sample_article, draft=sample_draft)

    @pytest.mark.asyncio
    async def test_llm_error_preserves_original(self, knowledge_loader):
        """LLMError 保留原始异常"""
        original = RuntimeError("network error")
        llm = MagicMock()
        llm.temperature = None
        llm.ainvoke = AsyncMock(side_effect=original)
        agent = DraftChatAgent(llm=llm, knowledge_loader=knowledge_loader)

        with pytest.raises(LLMError) as exc_info:
            await agent.answer(message="问题")
        assert exc_info.value.__cause__ is original
