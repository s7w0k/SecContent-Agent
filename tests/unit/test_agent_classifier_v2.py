"""
6分类 Agent — 单元测试

运行:
    pytest tests/unit/test_agent_classifier_v2.py -v
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
def sample_article():
    """测试用文章数据 — 典型的爆点事件"""
    return {
        "title": "Critical RCE Vulnerability in MCP Server Exposes Millions of Agents",
        "url": "https://example.com/mcp-rce",
        "source": "The Hacker News",
        "source_type": "overseas_news",
        "summary": "A critical remote code execution vulnerability was discovered...",
        "summary_cn": "MCP服务器中发现严重RCE漏洞，影响数百万智能体",
        "content_md": "## Overview\n\nSecurity researchers at Wiz have discovered...",
        "category": "MCP协议漏洞",
        "is_ai_security": True,
        "is_agent_security": True,
    }


@pytest.fixture
def sample_article_competitor():
    """测试用文章数据 — 竞品信息"""
    return {
        "title": "Palo Alto Networks Raises $500M for AI Security Platform",
        "url": "https://example.com/panw-funding",
        "source": "TechCrunch",
        "source_type": "overseas_news",
        "summary": "Palo Alto Networks announced a $500M funding round for AI security...",
        "summary_cn": "Palo Alto Networks获5亿美元融资用于AI安全平台，聚焦智能体安全",
        "content_md": "",
    }


@pytest.fixture
def sample_article_ambiguous():
    """测试用 — 模糊内容"""
    return {
        "title": "Some Random Tech News",
        "url": "https://example.com/random",
        "source": "Unknown",
        "summary": "Some random content that doesn't clearly fit any category...",
        "content_md": "",
    }


@pytest.fixture
def mock_llm_breaking():
    """Mock LLM 返回爆点事件"""
    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(return_value=AIMessage(content=json.dumps({
        "category": "爆点事件",
        "confidence": 92,
        "reason": "MCP RCE漏洞属于重大安全突发事件",
    })))
    return llm


@pytest.fixture
def mock_llm_competitor():
    """Mock LLM 返回竞品信息"""
    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(return_value=AIMessage(content=json.dumps({
        "category": "国内外竞品信息",
        "confidence": 85,
        "reason": "友商融资动态",
    })))
    return llm


@pytest.fixture
def mock_llm_ambiguous():
    """Mock LLM 返回模糊结果（低置信度）"""
    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(return_value=AIMessage(content=json.dumps({
        "category": "学术/会展/高校",
        "confidence": 30,
        "reason": "内容模糊，无法确定具体类别",
    })))
    return llm


@pytest.fixture
def classifier_breaking(mock_llm_breaking):
    """创建测试用 ClassifierV2（爆点事件 mock）"""
    from agent.classifier_v2 import ClassifierV2
    return ClassifierV2(llm=mock_llm_breaking)


@pytest.fixture
def classifier_competitor(mock_llm_competitor):
    """创建测试用 ClassifierV2（竞品 mock）"""
    from agent.classifier_v2 import ClassifierV2
    return ClassifierV2(llm=mock_llm_competitor)


# ═══════════════════════════════════════════════════════════════
# 1. CategoryV2 枚举测试
# ═══════════════════════════════════════════════════════════════


class TestCategoryV2Enum:
    """6分类枚举验证"""

    def test_all_six_categories_defined(self):
        from agent.classifier_v2 import CategoryV2
        values = CategoryV2.valid_values()
        assert len(values) == 6
        assert "爆点事件" in values
        assert "法律法规/监管动态" in values
        assert "AI技术重大进展" in values
        assert "国内外竞品信息" in values
        assert "运营商/行业事件" in values
        assert "学术/会展/高校" in values

    def test_pr_eligible_categories(self):
        from agent.classifier_v2 import CategoryV2
        pr_eligible = CategoryV2.pr_eligible()
        assert len(pr_eligible) == 3
        assert "爆点事件" in pr_eligible
        assert "法律法规/监管动态" in pr_eligible
        assert "AI技术重大进展" in pr_eligible
        assert "国内外竞品信息" not in pr_eligible
        assert "运营商/行业事件" not in pr_eligible
        assert "学术/会展/高校" not in pr_eligible

    def test_default_category(self):
        from agent.classifier_v2 import CategoryV2
        assert CategoryV2.default() == "不相关"

    def test_not_relevant_category(self):
        from agent.classifier_v2 import CategoryV2
        assert CategoryV2.NOT_RELEVANT.value == "不相关"
        assert "不相关" not in CategoryV2.valid_values()
        assert "不相关" not in CategoryV2.pr_eligible()

    def test_enum_values_match_chinese(self):
        from agent.classifier_v2 import CategoryV2
        assert CategoryV2.BREAKING_EVENT.value == "爆点事件"
        assert CategoryV2.LAW_AND_REGULATION.value == "法律法规/监管动态"
        assert CategoryV2.AI_TECH_PROGRESS.value == "AI技术重大进展"
        assert CategoryV2.COMPETITOR.value == "国内外竞品信息"
        assert CategoryV2.INDUSTRY_EVENT.value == "运营商/行业事件"
        assert CategoryV2.ACADEMIC.value == "学术/会展/高校"


# ═══════════════════════════════════════════════════════════════
# 2. Prompt 构建测试
# ═══════════════════════════════════════════════════════════════


class TestPromptBuilding:
    """Prompt 构建验证"""

    def test_system_prompt_contains_all_categories(self):
        from agent.classifier_v2 import SYSTEM_PROMPT
        assert "爆点事件" in SYSTEM_PROMPT
        assert "法律法规/监管动态" in SYSTEM_PROMPT
        assert "AI技术重大进展" in SYSTEM_PROMPT
        assert "国内外竞品信息" in SYSTEM_PROMPT
        assert "运营商/行业事件" in SYSTEM_PROMPT
        assert "学术/会展/高校" in SYSTEM_PROMPT

    def test_system_prompt_has_output_format(self):
        from agent.classifier_v2 import SYSTEM_PROMPT
        assert "category" in SYSTEM_PROMPT
        assert "confidence" in SYSTEM_PROMPT
        assert "reason" in SYSTEM_PROMPT
        assert "JSON" in SYSTEM_PROMPT

    def test_system_prompt_has_validation_rule(self):
        from agent.classifier_v2 import SYSTEM_PROMPT
        assert "必须严格等于上述6类之一" in SYSTEM_PROMPT

    def test_user_prompt_contains_article_fields(self, sample_article):
        from agent.classifier_v2 import ClassifierV2
        prompt = ClassifierV2._build_user_prompt(sample_article)
        assert "Critical RCE Vulnerability" in prompt
        assert "The Hacker News" in prompt
        assert "MCP服务器中发现严重RCE漏洞" in prompt

    def test_user_prompt_handles_missing_fields(self):
        from agent.classifier_v2 import ClassifierV2
        minimal = {"title": "X", "source": "Y"}
        prompt = ClassifierV2._build_user_prompt(minimal)
        assert "X" in prompt
        assert "Y" in prompt
        assert "无" in prompt  # default summary

    def test_user_prompt_falls_back_to_english_summary(self):
        from agent.classifier_v2 import ClassifierV2
        article = {
            "title": "Test",
            "source": "Src",
            "summary": "English summary here",
        }
        prompt = ClassifierV2._build_user_prompt(article)
        assert "English summary here" in prompt

    def test_user_prompt_prefers_chinese_summary(self):
        from agent.classifier_v2 import ClassifierV2
        article = {
            "title": "Test",
            "source": "Src",
            "summary": "English summary",
            "summary_cn": "中文摘要",
        }
        prompt = ClassifierV2._build_user_prompt(article)
        assert "中文摘要" in prompt
        # Should prefer Chinese summary over English
        assert prompt.index("中文摘要") < prompt.index("English summary")


# ═══════════════════════════════════════════════════════════════
# 3. JSON 响应解析测试
# ═══════════════════════════════════════════════════════════════


class TestResponseParsing:
    """LLM 响应解析验证"""

    def test_parse_json_code_block(self):
        from agent.classifier_v2 import ClassifierV2
        text = '```json\n{"category": "爆点事件", "confidence": 90, "reason": "重大漏洞"}\n```'
        result = ClassifierV2._parse_response(text)
        assert result["category"] == "爆点事件"
        assert result["confidence"] == 90

    def test_parse_plain_json(self):
        from agent.classifier_v2 import ClassifierV2
        text = '{"category": "AI技术重大进展", "confidence": 75, "reason": "新模型发布"}'
        result = ClassifierV2._parse_response(text)
        assert result["category"] == "AI技术重大进展"

    def test_parse_json_in_text(self):
        from agent.classifier_v2 import ClassifierV2
        text = '分析结果：{"category": "法律法规/监管动态", "confidence": 88, "reason": "新法规出台"}。以上。'
        result = ClassifierV2._parse_response(text)
        assert result["category"] == "法律法规/监管动态"

    def test_parse_invalid_raises(self):
        from agent.classifier_v2 import ClassifierV2
        with pytest.raises(ValueError, match="Cannot extract"):
            ClassifierV2._parse_response("No JSON here at all")

    def test_parse_code_block_without_json_tag(self):
        from agent.classifier_v2 import ClassifierV2
        text = '```\n{"category": "运营商/行业事件", "confidence": 60, "reason": "电信安全"}\n```'
        result = ClassifierV2._parse_response(text)
        assert result["category"] == "运营商/行业事件"


# ═══════════════════════════════════════════════════════════════
# 4. 分类结果校验测试
# ═══════════════════════════════════════════════════════════════


class TestResultValidation:
    """分类结果校验和修正"""

    def test_valid_category_passes_through(self):
        from agent.classifier_v2 import ClassifierV2
        parsed = {"category": "爆点事件", "confidence": 85, "reason": "ok"}
        result = ClassifierV2._validate_and_fix(parsed)
        assert result["category"] == "爆点事件"
        assert result["confidence"] == 85
        assert result["reason"] == "ok"

    def test_invalid_category_falls_back(self):
        from agent.classifier_v2 import ClassifierV2
        parsed = {"category": "不存在的类别", "confidence": 80, "reason": "..."}
        result = ClassifierV2._validate_and_fix(parsed)
        assert result["category"] == "不相关"  # default fallback

    def test_empty_category_falls_back(self):
        from agent.classifier_v2 import ClassifierV2
        parsed = {"category": "", "confidence": 80, "reason": "..."}
        result = ClassifierV2._validate_and_fix(parsed)
        assert result["category"] == "不相关"

    def test_confidence_clamped_to_max(self):
        from agent.classifier_v2 import ClassifierV2
        parsed = {"category": "爆点事件", "confidence": 999, "reason": "ok"}
        result = ClassifierV2._validate_and_fix(parsed)
        assert result["confidence"] == 100

    def test_confidence_clamped_to_min(self):
        from agent.classifier_v2 import ClassifierV2
        parsed = {"category": "爆点事件", "confidence": -50, "reason": "ok"}
        result = ClassifierV2._validate_and_fix(parsed)
        assert result["confidence"] == 0

    def test_missing_confidence_gets_default(self):
        from agent.classifier_v2 import ClassifierV2
        parsed = {"category": "国内外竞品信息", "reason": "ok"}
        result = ClassifierV2._validate_and_fix(parsed)
        assert result["confidence"] == 50

    def test_non_numeric_confidence_gets_default(self):
        from agent.classifier_v2 import ClassifierV2
        parsed = {"category": "国内外竞品信息", "confidence": "high", "reason": "ok"}
        result = ClassifierV2._validate_and_fix(parsed)
        assert result["confidence"] == 50

    def test_reason_truncated(self):
        from agent.classifier_v2 import ClassifierV2
        long_reason = "x" * 200
        parsed = {"category": "爆点事件", "confidence": 80, "reason": long_reason}
        result = ClassifierV2._validate_and_fix(parsed)
        assert len(result["reason"]) <= 100

    def test_all_six_categories_validated(self):
        from agent.classifier_v2 import CategoryV2, ClassifierV2
        for cat in CategoryV2.valid_values():
            parsed = {"category": cat, "confidence": 70, "reason": "test"}
            result = ClassifierV2._validate_and_fix(parsed)
            assert result["category"] == cat


# ═══════════════════════════════════════════════════════════════
# 5. ClassifyResultV2 测试
# ═══════════════════════════════════════════════════════════════


class TestClassifyResultV2:
    """分类结果对象验证"""

    def test_is_pr_eligible_breaking(self):
        from agent.classifier_v2 import ClassifyResultV2
        r = ClassifyResultV2(category="爆点事件", confidence=90, reason="test")
        assert r.is_pr_eligible is True

    def test_is_pr_eligible_law(self):
        from agent.classifier_v2 import ClassifyResultV2
        r = ClassifyResultV2(category="法律法规/监管动态", confidence=85, reason="test")
        assert r.is_pr_eligible is True

    def test_is_pr_eligible_ai_tech(self):
        from agent.classifier_v2 import ClassifyResultV2
        r = ClassifyResultV2(category="AI技术重大进展", confidence=80, reason="test")
        assert r.is_pr_eligible is True

    def test_is_pr_eligible_competitor_false(self):
        from agent.classifier_v2 import ClassifyResultV2
        r = ClassifyResultV2(category="国内外竞品信息", confidence=70, reason="test")
        assert r.is_pr_eligible is False

    def test_is_fallback_flag(self):
        from agent.classifier_v2 import ClassifyResultV2
        r = ClassifyResultV2(fallback=True)
        assert r.is_fallback is True

    def test_to_dict(self):
        from agent.classifier_v2 import ClassifyResultV2
        r = ClassifyResultV2(category="爆点事件", confidence=92, reason="重大漏洞", fallback=False)
        d = r.to_dict()
        assert d["category_v2"] == "爆点事件"
        assert d["category_v2_confidence"] == 92
        assert d["category_v2_reason"] == "重大漏洞"
        assert d["category_v2_fallback"] is False
        assert d["is_pr_eligible"] is True


# ═══════════════════════════════════════════════════════════════
# 6. 分类流程测试（mock LLM）
# ═══════════════════════════════════════════════════════════════


class TestClassificationFlow:
    """分类核心流程测试"""

    @pytest.mark.asyncio
    async def test_classify_single_breaking_event(self, classifier_breaking, sample_article):
        result = await classifier_breaking.classify_single(sample_article)
        assert result.category == "爆点事件"
        assert result.confidence == 92
        assert result.is_pr_eligible is True
        assert result.is_fallback is False

    @pytest.mark.asyncio
    async def test_classify_single_competitor(self, classifier_competitor, sample_article_competitor):
        result = await classifier_competitor.classify_single(sample_article_competitor)
        assert result.category == "国内外竞品信息"
        assert result.confidence == 85
        assert result.is_pr_eligible is False

    @pytest.mark.asyncio
    async def test_classify_batch(self, classifier_breaking):
        articles = [
            {"title": f"MCP漏洞 Article {i}", "source": "S", "summary": f"安全事件 Summary {i}"}
            for i in range(5)
        ]
        results = await classifier_breaking.classify_batch(articles)
        assert len(results) == 5
        for r in results:
            assert r.category == "爆点事件"
            assert not r.is_fallback

    @pytest.mark.asyncio
    async def test_classify_batch_empty(self, classifier_breaking):
        results = await classifier_breaking.classify_batch([])
        assert results == []

    @pytest.mark.asyncio
    async def test_classify_batch_preserves_order(self, mock_llm_breaking):
        from agent.classifier_v2 import ClassifierV2

        call_order = []

        async def track_order(*args, **kwargs):
            # Extract user message to identify which article
            msgs = kwargs.get("messages", args[0] if args else [])
            for m in msgs:
                if hasattr(m, "content") and "Article" in m.content:
                    import re as _re
                    idx_match = _re.search(r"Article (\d+)", m.content)
                    if idx_match:
                        call_order.append(int(idx_match.group(1)))
            return AIMessage(content=json.dumps({
                "category": "爆点事件", "confidence": 80, "reason": "test",
            }))

        mock_llm_breaking.ainvoke = AsyncMock(side_effect=track_order)
        classifier = ClassifierV2(llm=mock_llm_breaking)

        articles = [{"title": f"安全漏洞 Article {i}", "source": "S", "summary": ""} for i in range(3)]
        results = await classifier.classify_batch(articles)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_classify_fallback_on_llm_error(self, sample_article):
        from agent.classifier_v2 import ClassifierV2

        mock_llm = MagicMock()
        mock_llm.temperature = None
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("API timeout"))

        classifier = ClassifierV2(llm=mock_llm)
        result = await classifier.classify_single(sample_article)
        assert result.is_fallback is True
        assert "API timeout" in result.reason
        assert result.category == "不相关"  # default fallback
        assert result.confidence == 0

    @pytest.mark.asyncio
    async def test_classify_retry_then_succeed(self, sample_article):
        from agent.classifier_v2 import ClassifierV2

        mock_llm = MagicMock()
        mock_llm.temperature = None
        mock_llm.ainvoke = AsyncMock(side_effect=[
            Exception("Temporary error"),
            AIMessage(content=json.dumps({
                "category": "爆点事件", "confidence": 88, "reason": "恢复后分类",
            })),
        ])
        classifier = ClassifierV2(llm=mock_llm)
        result = await classifier.classify_single(sample_article)
        assert result.is_fallback is False
        assert result.category == "爆点事件"

    @pytest.mark.asyncio
    async def test_classify_with_pydantic_model(self, mock_llm_breaking):
        """测试 Pydantic model 输入（模拟 ArticleInDB）"""
        from agent.classifier_v2 import ClassifierV2

        class MockArticle:
            def model_dump(self):
                return {
                    "title": "Test MCP Vulnerability",
                    "source": "Test Source",
                    "summary": "A test article about MCP security",
                    "summary_cn": "",
                    "content_md": "",
                }

        classifier = ClassifierV2(llm=mock_llm_breaking)
        result = await classifier.classify_single(MockArticle())
        assert result.category == "爆点事件"

    @pytest.mark.asyncio
    async def test_classify_ambiguous_content(self, mock_llm_ambiguous, sample_article_ambiguous):
        """非安全相关文章被预筛选跳过，不调 LLM，直接标记为不相关"""
        from agent.classifier_v2 import ClassifierV2
        classifier = ClassifierV2(llm=mock_llm_ambiguous)
        result = await classifier.classify_single(sample_article_ambiguous)
        assert result.category == "不相关"
        assert result.confidence == 100  # 预筛选标记，非 LLM 输出
        assert result.is_pr_eligible is False
        assert result.is_fallback is False

    @pytest.mark.asyncio
    async def test_batch_with_mixed_results(self, sample_article, sample_article_competitor):
        """批量分类返回混合结果"""
        from agent.classifier_v2 import ClassifierV2

        # Mock 根据不同文章返回不同结果
        async def mixed_response(*args, **kwargs):
            msgs = args[0] if args else kwargs.get("messages", [])
            content_str = ""
            for m in msgs:
                if hasattr(m, "content"):
                    content_str += m.content
            if "Palo Alto" in content_str:
                return AIMessage(content=json.dumps({
                    "category": "国内外竞品信息", "confidence": 85, "reason": "融资",
                }))
            else:
                return AIMessage(content=json.dumps({
                    "category": "爆点事件", "confidence": 92, "reason": "漏洞",
                }))

        mock_llm = MagicMock()
        mock_llm.temperature = None
        mock_llm.ainvoke = AsyncMock(side_effect=mixed_response)

        classifier = ClassifierV2(llm=mock_llm)
        results = await classifier.classify_batch([sample_article, sample_article_competitor])

        assert len(results) == 2
        assert results[0].category == "爆点事件"
        assert results[1].category == "国内外竞品信息"


# ═══════════════════════════════════════════════════════════════
# 7. 常量/配置测试
# ═══════════════════════════════════════════════════════════════


class TestConstants:
    """常量和配置验证"""

    def test_default_temperature_low(self):
        from agent.classifier_v2 import DEFAULT_TEMPERATURE
        assert DEFAULT_TEMPERATURE == 0.1  # 分类需要低温度确保一致性

    def test_max_retries(self):
        from agent.classifier_v2 import MAX_RETRIES
        assert MAX_RETRIES >= 1

    def test_confidence_bounds(self):
        from agent.classifier_v2 import CONFIDENCE_MAX, CONFIDENCE_MIN
        assert CONFIDENCE_MIN == 0
        assert CONFIDENCE_MAX == 100
