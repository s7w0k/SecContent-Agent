"""
双维度打分 Agent — 单元测试

运行:
    pytest tests/unit/test_agent_scorer.py -v
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def knowledge():
    """提供测试用知识库"""
    from agent.knowledge import ProductKnowledge

    return ProductKnowledge(
        product_positioning="测试产品定位：智能体身份安全",
        core_features=["MCP协议安全防护", "智能体身份认证", "意图识别"],
        tech_barriers=["动态上下文感知", "多模态意图理解"],
        control_points=["首家MCP安全审计", "运营商级验证"],
    )


@pytest.fixture
def sample_article():
    """测试用文章数据"""
    return {
        "title": "Critical MCP Vulnerability Exposes Agent Authentication",
        "url": "https://example.com/mcp-vuln",
        "source": "The Hacker News",
        "source_type": "overseas_news",
        "summary": "A critical vulnerability in MCP protocol servers...",
        "summary_cn": "MCP服务器中发现严重漏洞",
        "category": "MCP协议漏洞",
        "is_ai_security": True,
        "is_agent_security": True,
    }


@pytest.fixture
def mock_llm():
    """创建 mock LLM，返回预定义打分结果"""
    llm = MagicMock()
    llm.temperature = None  # 将被 scorer 覆盖
    llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "ai_relevance_score": 92,
                    "reportability_score": 78,
                    "score_reason": "MCP协议漏洞直接涉及Agent身份安全核心领域",
                    "tags": ["MCP协议", "身份认证", "漏洞披露"],
                }
            )
        )
    )
    return llm


@pytest.fixture
def scorer(mock_llm, knowledge):
    """创建测试用 ScoringAgent"""
    from agent.scorer import ScoringAgent

    return ScoringAgent(llm=mock_llm, knowledge=knowledge)


# ═══════════════════════════════════════════════════════════════
# 1. System Prompt 构建测试
# ═══════════════════════════════════════════════════════════════


class TestPromptBuilding:
    """Prompt 构建验证"""

    def test_system_prompt_contains_knowledge(self, scorer):
        prompt = scorer.system_prompt
        assert "智能体身份安全" in prompt
        assert "MCP协议" in prompt
        assert "ai_relevance_score" in prompt
        assert "reportability_score" in prompt

    def test_system_prompt_has_scoring_dimensions(self, scorer):
        prompt = scorer.system_prompt
        assert "0-100" in prompt
        assert "AI/Agent安全相关度" in prompt
        assert "可报道性" in prompt

    def test_system_prompt_has_output_format(self, scorer):
        prompt = scorer.system_prompt
        assert "ai_relevance_score" in prompt
        assert "reportability_score" in prompt
        assert "score_reason" in prompt
        assert "tags" in prompt

    def test_user_prompt_contains_article_info(self, sample_article):
        from agent.scorer import ScoringAgent

        prompt = ScoringAgent._build_user_prompt(sample_article)
        assert "Critical MCP Vulnerability" in prompt
        assert "The Hacker News" in prompt
        assert "MCP协议漏洞" in prompt

    def test_user_prompt_shows_ai_flags(self, sample_article):
        from agent.scorer import ScoringAgent

        prompt = ScoringAgent._build_user_prompt(sample_article)
        assert "Critical MCP Vulnerability" in prompt
        assert "MCP协议漏洞" in prompt  # category field is in the prompt

    def test_user_prompt_handles_missing_fields(self):
        from agent.scorer import ScoringAgent

        minimal = {"title": "X", "source": "Y", "category": "", "summary": ""}
        prompt = ScoringAgent._build_user_prompt(minimal)
        assert "X" in prompt
        assert "未分类" in prompt  # default when category is empty


# ═══════════════════════════════════════════════════════════════
# 2. JSON 解析测试
# ═══════════════════════════════════════════════════════════════


class TestResponseParsing:
    """LLM 响应解析验证"""

    def test_parse_json_code_block(self):
        from agent.scorer import ScoringAgent

        text = '```json\n{"ai_relevance_score": 85, "reportability_score": 70, "score_reason": "测试", "tags": ["MCP"]}\n```'
        result = ScoringAgent._parse_response(text)
        assert result["ai_relevance_score"] == 85
        assert result["reportability_score"] == 70

    def test_parse_plain_json(self):
        from agent.scorer import ScoringAgent

        text = '{"ai_relevance_score": 60, "reportability_score": 40, "score_reason": "ok", "tags": []}'
        result = ScoringAgent._parse_response(text)
        assert result["ai_relevance_score"] == 60

    def test_parse_json_in_text(self):
        from agent.scorer import ScoringAgent

        text = 'Here is my analysis: {"ai_relevance_score": 50, "reportability_score": 30, "score_reason": "...", "tags": ["x"]}. That is all.'
        result = ScoringAgent._parse_response(text)
        assert result["ai_relevance_score"] == 50

    def test_parse_invalid_raises(self):
        from agent.scorer import ScoringAgent

        with pytest.raises(ValueError, match="Cannot extract"):
            ScoringAgent._parse_response("No JSON here at all")

    def test_parse_json_without_code_fence_marker(self):
        from agent.scorer import ScoringAgent

        text = '```\n{"ai_relevance_score": 75, "reportability_score": 65, "score_reason": "x", "tags": []}\n```'
        result = ScoringAgent._parse_response(text)
        assert result["ai_relevance_score"] == 75


# ═══════════════════════════════════════════════════════════════
# 3. 分数校验测试
# ═══════════════════════════════════════════════════════════════


class TestScoreValidation:
    """分数校验和修正"""

    def test_valid_scores_pass_through(self):
        from agent.scorer import ScoringAgent

        parsed = {
            "ai_relevance_score": 80,
            "reportability_score": 60,
            "score_reason": "ok",
            "tags": ["x"],
        }
        result = ScoringAgent._validate_and_fix(parsed)
        assert result["ai_relevance_score"] == 80
        assert result["reportability_score"] == 60

    def test_score_clamped_to_max(self):
        from agent.scorer import ScoringAgent

        parsed = {
            "ai_relevance_score": 150,
            "reportability_score": 200,
            "score_reason": "ok",
            "tags": [],
        }
        result = ScoringAgent._validate_and_fix(parsed)
        assert result["ai_relevance_score"] == 100
        assert result["reportability_score"] == 100

    def test_score_clamped_to_min(self):
        from agent.scorer import ScoringAgent

        parsed = {
            "ai_relevance_score": -10,
            "reportability_score": -50,
            "score_reason": "ok",
            "tags": [],
        }
        result = ScoringAgent._validate_and_fix(parsed)
        assert result["ai_relevance_score"] == 0
        assert result["reportability_score"] == 0

    def test_missing_fields_get_defaults(self):
        from agent.scorer import ScoringAgent

        parsed: dict = {}
        result = ScoringAgent._validate_and_fix(parsed)
        assert result["ai_relevance_score"] == 0
        assert result["reportability_score"] == 0
        assert result["score_reason"] == ""

    def test_tags_converted_to_list(self):
        from agent.scorer import ScoringAgent

        parsed = {
            "ai_relevance_score": 50,
            "reportability_score": 50,
            "score_reason": "x",
            "tags": "not a list",
        }
        result = ScoringAgent._validate_and_fix(parsed)
        assert isinstance(result["tags"], list)
        assert len(result["tags"]) == 1

    def test_tags_truncated_to_5(self):
        from agent.scorer import ScoringAgent

        parsed = {
            "ai_relevance_score": 50,
            "reportability_score": 50,
            "score_reason": "x",
            "tags": ["a", "b", "c", "d", "e", "f", "g"],
        }
        result = ScoringAgent._validate_and_fix(parsed)
        assert len(result["tags"]) <= 5


# ═══════════════════════════════════════════════════════════════
# 4. 打分流程测试（mock LLM）
# ═══════════════════════════════════════════════════════════════


class TestScoringFlow:
    """打分核心流程测试"""

    @pytest.mark.asyncio
    async def test_score_single_success(self, scorer, sample_article):
        result = await scorer.score_single(sample_article)
        assert result["ai_relevance_score"] == 92
        assert result["reportability_score"] == 78
        assert result["total_score"] == 170
        assert result["is_high_value"] is True
        assert result["_fallback"] is False
        assert len(result["tags"]) > 0

    @pytest.mark.asyncio
    async def test_score_single_below_threshold(self, mock_llm, knowledge, sample_article):
        from agent.scorer import ScoringAgent

        # Mock 返回低分
        mock_llm.ainvoke = AsyncMock(
            return_value=AIMessage(
                content=json.dumps(
                    {
                        "ai_relevance_score": 30,
                        "reportability_score": 20,
                        "score_reason": "Not relevant",
                        "tags": [],
                    }
                )
            )
        )
        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        result = await scorer.score_single(sample_article)
        assert result["is_high_value"] is False
        assert result["total_score"] == 50

    @pytest.mark.asyncio
    async def test_score_batch_concurrent(self, scorer):
        articles = [
            {"title": f"Article {i}", "source": "S", "category": "", "summary": ""}
            for i in range(5)
        ]
        results = await scorer.score_batch(articles)
        assert len(results) == 5
        for r in results:
            assert "ai_relevance_score" in r
            assert "total_score" in r

    @pytest.mark.asyncio
    async def test_score_batch_empty(self, scorer):
        results = await scorer.score_batch([])
        assert results == []

    @pytest.mark.asyncio
    async def test_score_fallback_on_llm_error(self, mock_llm, knowledge, sample_article):
        from agent.scorer import ScoringAgent

        mock_llm.ainvoke = AsyncMock(side_effect=Exception("API timeout"))
        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        result = await scorer.score_single(sample_article)
        assert result["_fallback"] is True
        assert "API timeout" in result["score_reason"]
        assert result["total_score"] == 0

    @pytest.mark.asyncio
    async def test_score_retry_then_succeed(self, mock_llm, knowledge, sample_article):
        from agent.scorer import ScoringAgent

        # 第一次失败，第二次成功
        mock_llm.ainvoke = AsyncMock(
            side_effect=[
                Exception("Temporary error"),
                AIMessage(
                    content=json.dumps(
                        {
                            "ai_relevance_score": 88,
                            "reportability_score": 66,
                            "score_reason": "Recovered",
                            "tags": ["测试"],
                        }
                    )
                ),
            ]
        )
        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        result = await scorer.score_single(sample_article)
        assert result["_fallback"] is False
        assert result["ai_relevance_score"] == 88

    @pytest.mark.asyncio
    async def test_score_dict_article(self, scorer):
        """测试 dict 格式文章输入"""
        result = await scorer.score_single(
            {
                "title": "T",
                "source": "S",
                "category": "C",
                "summary": "Sum",
                "is_ai_security": False,
                "is_agent_security": False,
            }
        )
        assert result["ai_relevance_score"] == 92  # mock returns this

    @pytest.mark.asyncio
    async def test_enrich_result(self):
        from agent.scorer import ScoringAgent

        validated = {
            "ai_relevance_score": 85,
            "reportability_score": 72,
            "score_reason": "ok",
            "tags": [],
        }
        result = ScoringAgent._enrich_result(validated)
        assert result["total_score"] == 157
        assert result["is_high_value"] is True
        assert result["_fallback"] is False

    @pytest.mark.asyncio
    async def test_fallback_score_structure(self):
        from agent.scorer import ScoringAgent

        result = ScoringAgent._fallback_score("test error")
        assert result["_fallback"] is True
        assert result["total_score"] == 0
        assert "test error" in result["score_reason"]
