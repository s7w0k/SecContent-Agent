"""
PR 模板库 + 草稿生成器 — 单元测试

运行:
    pytest tests/unit/test_pr_templates.py -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


# ═══════════════════════════════════════════════════════════════
# 1. PRTemplate 模板库测试
# ═══════════════════════════════════════════════════════════════


class TestPRTemplates:
    """6套模板结构与匹配"""

    def test_all_six_templates_defined(self):
        from agent.pr_templates import PR_TEMPLATES

        assert len(PR_TEMPLATES) == 3
        assert "爆点事件" in PR_TEMPLATES
        assert "法律法规/监管动态" in PR_TEMPLATES
        assert "AI技术重大进展" in PR_TEMPLATES

        for cat, tmpls in PR_TEMPLATES.items():
            assert len(tmpls) == 2, f"{cat} should have 2 templates"

    def test_template_has_required_fields(self):
        from agent.pr_templates import PR_TEMPLATES

        for cat, tmpls in PR_TEMPLATES.items():
            for tpl in tmpls:
                assert tpl.name, f"Template in {cat} has no name"
                assert tpl.title_template, f"{tpl.name} has no title_template"
                assert len(tpl.sections) >= 4, f"{tpl.name} needs >=4 sections"
                assert len(tpl.perspectives) == 2, f"{tpl.name} needs 2 perspectives"

    def test_system_templates_have_stable_identity(self):
        from agent.pr_templates import PR_TEMPLATES, SYSTEM_TEMPLATES_BY_KEY

        expected = {
            "breaking_a": ("爆点事件", "A"),
            "breaking_b": ("爆点事件", "B"),
            "law_a": ("法律法规/监管动态", "A"),
            "law_b": ("法律法规/监管动态", "B"),
            "ai_a": ("AI技术重大进展", "A"),
            "ai_b": ("AI技术重大进展", "B"),
        }

        assert set(SYSTEM_TEMPLATES_BY_KEY) == set(expected)
        for templates in PR_TEMPLATES.values():
            for template in templates:
                assert (template.category, template.slot) == expected[template.template_key]
                assert template.system_version == 1

    def test_get_system_template_by_key(self):
        from agent.pr_templates import get_system_template

        assert get_system_template("breaking_a").name == "爆点A"
        assert get_system_template("missing") is None

    def test_template_sections_have_headings(self):
        from agent.pr_templates import PR_TEMPLATES

        for tmpls in PR_TEMPLATES.values():
            for tpl in tmpls:
                for sec in tpl.sections:
                    assert "heading" in sec
                    assert "guide" in sec

    def test_build_system_prompt(self):
        from agent.pr_templates import PR_TEMPLATES

        tpl = PR_TEMPLATES["爆点事件"][0]
        prompt = tpl.build_system_prompt("测试角度")
        assert "爆点A" in prompt
        assert "测试角度" in prompt
        assert "事件概述" in prompt

    def test_match_templates_breaking(self):
        from agent.pr_templates import match_templates

        result = match_templates("爆点事件")
        assert len(result) == 2
        assert result[0].name == "爆点A"
        assert result[1].name == "爆点B"

    def test_match_templates_law(self):
        from agent.pr_templates import match_templates

        result = match_templates("法律法规/监管动态")
        assert len(result) == 2
        assert result[0].name == "法规A"

    def test_match_templates_ai(self):
        from agent.pr_templates import match_templates

        result = match_templates("AI技术重大进展")
        assert len(result) == 2
        assert result[0].name == "AI技术A"

    def test_match_templates_non_pr_category(self):
        from agent.pr_templates import match_templates

        # 非 PR 类别返回空列表
        assert match_templates("国内外竞品信息") == []
        assert match_templates("运营商/行业事件") == []
        assert match_templates("学术/会展/高校") == []
        assert match_templates("不存在的类别") == []

    def test_get_all_template_names(self):
        from agent.pr_templates import get_all_template_names

        names = get_all_template_names()
        assert len(names) == 3
        assert names["爆点事件"] == ["爆点A", "爆点B"]


# ═══════════════════════════════════════════════════════════════
# 2. DraftGenerator 测试
# ═══════════════════════════════════════════════════════════════


MOCK_DRAFT_CONTENT = """# [Critical MCP Vulnerability]：影响分析与产品应对建议

## 事件概述
近日发现MCP协议存在严重RCE漏洞，影响数百万智能体。

## 技术分析
该漏洞利用MCP服务器的认证缺陷实现远程代码执行。

## 产品关联
我们的MCP安全防护产品可直接检测并阻断此类攻击。

## 市场机会
可作为核心案例向客户展示产品在MCP安全方面的领先能力。

## 行动建议
1. 更新漏洞检测规则
2. 向客户推送安全公告
3. 纳入产品白皮书

## 关键词
MCP协议、RCE漏洞、身份安全、智能体防护
"""


@pytest.fixture
def knowledge():
    from agent.knowledge import ProductKnowledge

    return ProductKnowledge(
        product_positioning="智能体身份安全产品",
        core_features=["MCP协议安全防护", "智能体身份认证"],
        tech_barriers=["动态上下文感知"],
        control_points=["首家MCP安全审计"],
    )


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(return_value=AIMessage(content=MOCK_DRAFT_CONTENT))
    return llm


@pytest.fixture
def generator(mock_llm, knowledge):
    from agent.draft_generator import DraftGenerator

    return DraftGenerator(llm=mock_llm, knowledge=knowledge)


@pytest.fixture
def sample_article():
    return {
        "title": "Critical MCP Protocol RCE Vulnerability Discovered",
        "url": "https://example.com/mcp-rce",
        "source": "The Hacker News",
        "published_at": "2026-07-03",
        "summary": "A critical RCE vulnerability in MCP servers...",
        "content_md": "## Overview\nSecurity researchers found a critical RCE...",
        "category_v2": "爆点事件",
        "category_v2_confidence": 92,
        "is_pr_eligible": True,
    }


@pytest.fixture
def sample_scores():
    return {
        "product_relevance": 85,
        "event_impact": 72,
        "pr_total_score": 157,
        "score_reason": "MCP漏洞直接涉及产品核心能力",
        "tags": ["MCP协议", "RCE漏洞"],
    }


class TestDraftGenerator:
    """草稿生成器核心流程"""

    @pytest.mark.asyncio
    async def test_generate_4_drafts(self, generator, sample_article, sample_scores):
        result = await generator.generate(sample_article, sample_scores)
        assert result["ok"] is True
        assert len(result["drafts"]) == 4  # 2 templates × 2 perspectives
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_drafts_have_required_fields(self, generator, sample_article, sample_scores):
        result = await generator.generate(sample_article, sample_scores)
        for draft in result["drafts"]:
            assert "template" in draft
            assert "perspective" in draft
            assert "content_md" in draft
            assert "title" in draft
            assert "index" in draft

    @pytest.mark.asyncio
    async def test_drafts_use_both_templates(self, generator, sample_article, sample_scores):
        result = await generator.generate(sample_article, sample_scores)
        template_names = {d["template"] for d in result["drafts"]}
        assert "爆点A" in template_names
        assert "爆点B" in template_names

    @pytest.mark.asyncio
    async def test_non_pr_category_returns_empty(self, generator, mock_llm, knowledge):
        """非 PR 类别无模板匹配，返回空"""

        article = {
            "title": "Competitor raises funding",
            "source": "TC",
            "category_v2": "国内外竞品信息",
        }
        result = await generator.generate(article, {})
        assert result["ok"] is False
        assert result["drafts"] == []

    @pytest.mark.asyncio
    async def test_clean_draft_removes_code_fence(self):
        from agent.draft_generator import DraftGenerator

        text = "```markdown\n# Title\n\nContent\n```"
        cleaned = DraftGenerator._clean_draft(text, "Original Title")
        assert "```" not in cleaned

    @pytest.mark.asyncio
    async def test_clean_draft_adds_title_when_missing(self):
        from agent.draft_generator import DraftGenerator

        text = "## Section One\n\nContent here"
        cleaned = DraftGenerator._clean_draft(text, "My Title")
        assert cleaned.startswith("# ")

    @pytest.mark.asyncio
    async def test_user_prompt_contains_v2_scores(self, sample_article, sample_scores):
        from agent.draft_generator import DraftGenerator

        prompt = DraftGenerator._build_user_prompt(sample_article, sample_scores)
        assert "85/100" in prompt
        assert "72/100" in prompt
        assert "157/200" in prompt
        assert "爆点事件" in prompt

    def test_system_prompt_injects_style_hints(self, generator):
        from agent.pr_templates import PR_TEMPLATES

        tpl = PR_TEMPLATES["爆点事件"][0]
        prompt = generator._build_system_prompt(
            tpl,
            "市场传播视角",
            "## 用户风格偏好\n- 偏好语气：market_oriented",
        )
        assert "## 用户风格偏好" in prompt
        assert "market_oriented" in prompt
        assert "不要牺牲事实准确性" not in prompt

    def test_system_prompt_without_style_keeps_legacy_shape(self, generator):
        from agent.pr_templates import PR_TEMPLATES

        tpl = PR_TEMPLATES["爆点事件"][0]
        prompt = generator._build_system_prompt(tpl, "市场传播视角")
        assert "用户风格偏好" not in prompt
        assert "## 写作要求" in prompt

    @pytest.mark.asyncio
    async def test_fallback_draft_has_skeleton(self, sample_article):
        from agent.draft_generator import DraftGenerator
        from agent.pr_templates import PR_TEMPLATES

        tpl = PR_TEMPLATES["爆点事件"][0]
        draft = DraftGenerator._fallback_draft(
            sample_article,
            tpl,
            "测试角度",
            1,
            "Test error",
        )
        assert "待完善" in draft["content_md"]
        assert "Test error" in draft["content_md"]
        assert draft["template"] == "爆点A"
        assert draft["index"] == 1

    @pytest.mark.asyncio
    async def test_generate_with_ai_tech_category(self, mock_llm, knowledge):
        """AI技术重大进展分类"""
        from agent.draft_generator import DraftGenerator

        generator_ai = DraftGenerator(llm=mock_llm, knowledge=knowledge)
        article = {
            "title": "Claude 4 Released",
            "source": "TechCrunch",
            "category_v2": "AI技术重大进展",
            "summary": "Anthropic releases Claude 4...",
        }
        result = await generator_ai.generate(
            article,
            {
                "product_relevance": 70,
                "event_impact": 80,
                "pr_total_score": 150,
            },
        )
        assert result["ok"] is True
        assert len(result["drafts"]) == 4
        template_names = {d["template"] for d in result["drafts"]}
        assert "AI技术A" in template_names

    @pytest.mark.asyncio
    async def test_generate_with_law_category(self, mock_llm, knowledge):
        """法律法规分类"""
        from agent.draft_generator import DraftGenerator

        generator_law = DraftGenerator(llm=mock_llm, knowledge=knowledge)
        article = {
            "title": "New AI Regulation Passed",
            "source": "Reuters",
            "category_v2": "法律法规/监管动态",
            "summary": "EU passes new AI regulation...",
        }
        result = await generator_law.generate(
            article,
            {
                "product_relevance": 65,
                "event_impact": 60,
                "pr_total_score": 125,
            },
        )
        assert result["ok"] is True
        template_names = {d["template"] for d in result["drafts"]}
        assert "法规A" in template_names
