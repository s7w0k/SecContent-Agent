"""
双维度打分 Agent V2 — 单元测试

运行:
    pytest tests/unit/test_agent_scorer_v2.py -v
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
    """V2 知识库"""
    from agent.knowledge import ProductKnowledge

    return ProductKnowledge(
        product_positioning="智能体身份安全产品 — 定义AI时代的安全身份通行证",
        core_features=["MCP协议安全防护", "智能体身份认证", "意图识别"],
        tech_barriers=["动态上下文感知", "多模态意图理解"],
        control_points=["首家MCP安全审计", "运营商级验证"],
        customer_cases=["北京移动：家宽智能体身份防护"],
    )


@pytest.fixture
def sample_article():
    """PR 候选文章（爆点事件）"""
    return {
        "title": "Critical MCP Protocol RCE Vulnerability Discovered",
        "url": "https://example.com/mcp-rce",
        "source": "The Hacker News",
        "summary": "A critical RCE vulnerability in MCP servers...",
        "summary_cn": "MCP服务器中发现严重RCE漏洞",
        "content_md": "## Overview\nSecurity researchers found...",
        "category_v2": "爆点事件",
        "category_v2_confidence": 92,
        "is_pr_eligible": True,
    }


@pytest.fixture
def sample_article_ai_tech():
    """PR 候选文章（AI技术进展）"""
    return {
        "title": "Anthropic Releases Claude 4 with Advanced Agent Safety",
        "url": "https://example.com/claude4",
        "source": "TechCrunch",
        "summary": "Claude 4 introduces new agent safety features...",
        "summary_cn": "Claude 4发布，引入全新Agent安全能力",
        "category_v2": "AI技术重大进展",
        "is_pr_eligible": True,
    }


@pytest.fixture
def mock_llm_high():
    """Mock LLM 返回高分"""
    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "relevance": 85,
                    "event_impact": 72,
                    "reason": "MCP协议漏洞直接涉及产品核心能力，安全圈热传",
                }
            )
        )
    )
    return llm


@pytest.fixture
def mock_llm_low():
    """Mock LLM 返回低分（不达 PR 候选阈值）"""
    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "relevance": 25,
                    "event_impact": 30,
                    "reason": "与产品弱关联",
                }
            )
        )
    )
    return llm


@pytest.fixture
def scorer(mock_llm_high, knowledge):
    """测试用 ScoringAgentV2"""
    from agent.scorer_v2 import ScoringAgentV2

    return ScoringAgentV2(llm=mock_llm_high, knowledge=knowledge)


# ═══════════════════════════════════════════════════════════════
# 1. Prompt 构建测试
# ═══════════════════════════════════════════════════════════════


class TestPromptBuilding:
    """Prompt 构建验证"""

    def test_system_prompt_contains_knowledge(self, scorer):
        prompt = scorer.system_prompt
        assert "智能体身份安全" in prompt
        assert "MCP协议" in prompt
        assert "relevance" in prompt
        assert "event_impact" in prompt

    def test_system_prompt_has_scoring_rubric(self, scorer):
        prompt = scorer.system_prompt
        assert "90-100" in prompt
        assert "产品能力相关度" in prompt
        assert "事件影响面与传播力" in prompt

    def test_system_prompt_has_output_format(self, scorer):
        prompt = scorer.system_prompt
        assert "relevance" in prompt
        assert "event_impact" in prompt
        assert "reason" in prompt

    def test_user_prompt_contains_v2_category(self, sample_article):
        from agent.scorer_v2 import ScoringAgentV2

        prompt = ScoringAgentV2._build_user_prompt(sample_article)
        assert "爆点事件" in prompt  # V2 分类标签
        assert "Critical MCP" in prompt

    def test_user_prompt_handles_missing_fields(self):
        from agent.scorer_v2 import ScoringAgentV2

        minimal = {"title": "X", "source": "Y", "category_v2": "", "summary": ""}
        prompt = ScoringAgentV2._build_user_prompt(minimal)
        assert "X" in prompt
        assert "未分类" in prompt  # default when category_v2 empty


# ═══════════════════════════════════════════════════════════════
# 2. JSON 解析测试
# ═══════════════════════════════════════════════════════════════


class TestResponseParsing:
    """LLM 响应解析验证"""

    def test_parse_json_code_block(self):
        from agent.scorer_v2 import ScoringAgentV2

        text = '```json\n{"relevance": 85, "event_impact": 70, "reason": "ok"}\n```'
        result = ScoringAgentV2._parse_response(text)
        assert result["relevance"] == 85
        assert result["event_impact"] == 70

    def test_parse_plain_json(self):
        from agent.scorer_v2 import ScoringAgentV2

        text = '{"relevance": 60, "event_impact": 40, "reason": "test"}'
        result = ScoringAgentV2._parse_response(text)
        assert result["relevance"] == 60

    def test_parse_json_in_text(self):
        from agent.scorer_v2 import ScoringAgentV2

        text = '我的分析：{"relevance": 50, "event_impact": 30, "reason": "..."}。完毕。'
        result = ScoringAgentV2._parse_response(text)
        assert result["relevance"] == 50

    def test_parse_invalid_raises(self):
        from agent.scorer_v2 import ScoringAgentV2

        with pytest.raises(ValueError, match="Cannot extract"):
            ScoringAgentV2._parse_response("No JSON here at all")


# ═══════════════════════════════════════════════════════════════
# 3. 分数校验测试
# ═══════════════════════════════════════════════════════════════


class TestScoreValidation:
    """分数校验和修正"""

    def test_valid_scores_pass_through(self):
        from agent.scorer_v2 import ScoringAgentV2

        parsed = {"product_relevance": 80, "event_impact": 60, "reason": "ok", "tags": ["x"]}
        result = ScoringAgentV2._validate_and_fix(parsed)
        assert result["product_relevance"] == 80
        assert result["event_impact"] == 60

    def test_scores_clamped_to_max(self):
        from agent.scorer_v2 import ScoringAgentV2

        parsed = {"product_relevance": 150, "event_impact": 200, "reason": "ok", "tags": []}
        result = ScoringAgentV2._validate_and_fix(parsed)
        assert result["product_relevance"] == 100
        assert result["event_impact"] == 100

    def test_scores_clamped_to_min(self):
        from agent.scorer_v2 import ScoringAgentV2

        parsed = {"product_relevance": -10, "event_impact": -50, "reason": "ok", "tags": []}
        result = ScoringAgentV2._validate_and_fix(parsed)
        assert result["product_relevance"] == 0
        assert result["event_impact"] == 0

    def test_missing_fields_get_defaults(self):
        from agent.scorer_v2 import ScoringAgentV2

        parsed: dict = {}
        result = ScoringAgentV2._validate_and_fix(parsed)
        assert result["product_relevance"] == 0
        assert result["event_impact"] == 0
        assert result["score_reason"] == ""

    def test_tags_converted_to_list(self):
        from agent.scorer_v2 import ScoringAgentV2

        parsed = {"product_relevance": 50, "event_impact": 50, "reason": "x", "tags": "not a list"}
        result = ScoringAgentV2._validate_and_fix(parsed)
        assert isinstance(result["tags"], list)

    def test_tags_truncated_to_5(self):
        from agent.scorer_v2 import ScoringAgentV2

        parsed = {
            "product_relevance": 50,
            "event_impact": 50,
            "reason": "x",
            "tags": ["a", "b", "c", "d", "e", "f", "g"],
        }
        result = ScoringAgentV2._validate_and_fix(parsed)
        assert len(result["tags"]) <= 5


# ═══════════════════════════════════════════════════════════════
# 4. 打分流程测试（mock LLM）
# ═══════════════════════════════════════════════════════════════


class TestScoringFlow:
    """打分核心流程测试"""

    @pytest.mark.asyncio
    async def test_score_single_high(self, scorer, sample_article):
        result = await scorer.score_single(sample_article)
        assert result["product_relevance"] == 85
        assert result["event_impact"] == 72
        assert result["pr_total_score"] == 157
        assert result["is_pr_candidate"] is True
        assert result["_fallback"] is False

    @pytest.mark.asyncio
    async def test_score_single_below_threshold(self, mock_llm_low, knowledge, sample_article):
        from agent.scorer_v2 import ScoringAgentV2

        scorer_low = ScoringAgentV2(llm=mock_llm_low, knowledge=knowledge)
        result = await scorer_low.score_single(sample_article)
        assert result["is_pr_candidate"] is False
        assert result["pr_total_score"] == 55  # 25 + 30

    @pytest.mark.asyncio
    async def test_score_single_uses_adjusted_threshold(
        self, mock_llm_low, knowledge, sample_article
    ):
        from agent.scorer_v2 import ScoringAgentV2

        scorer_low = ScoringAgentV2(llm=mock_llm_low, knowledge=knowledge)
        result = await scorer_low.score_single(
            sample_article,
            threshold=50,
            threshold_adjustment=-30,
        )
        assert result["pr_total_score"] == 55
        assert result["is_pr_candidate"] is True
        assert result["pr_threshold"] == 50
        assert result["threshold_adjustment"] == -30

    @pytest.mark.asyncio
    async def test_score_batch(self, scorer):
        articles = [
            {"title": f"Article {i}", "source": "S", "category_v2": "爆点事件", "summary": ""}
            for i in range(5)
        ]
        results = await scorer.score_batch(articles)
        assert len(results) == 5
        for r in results:
            assert r["product_relevance"] == 85
            assert r["is_pr_candidate"] is True

    @pytest.mark.asyncio
    async def test_score_batch_empty(self, scorer):
        results = await scorer.score_batch([])
        assert results == []

    @pytest.mark.asyncio
    async def test_fallback_on_llm_error(self, mock_llm_high, knowledge, sample_article):
        from agent.scorer_v2 import ScoringAgentV2

        mock_llm_high.ainvoke = AsyncMock(side_effect=Exception("API timeout"))
        scorer_err = ScoringAgentV2(llm=mock_llm_high, knowledge=knowledge)
        result = await scorer_err.score_single(sample_article)
        assert result["_fallback"] is True
        assert "API timeout" in result["score_reason"]
        assert result["pr_total_score"] == 0
        assert result["is_pr_candidate"] is False

    @pytest.mark.asyncio
    async def test_retry_then_succeed(self, mock_llm_high, knowledge, sample_article):
        from agent.scorer_v2 import ScoringAgentV2

        mock_llm_high.ainvoke = AsyncMock(
            side_effect=[
                Exception("Temporary error"),
                AIMessage(
                    content=json.dumps(
                        {
                            "relevance": 88,
                            "event_impact": 66,
                            "reason": "恢复后打分",
                        }
                    )
                ),
            ]
        )
        scorer_retry = ScoringAgentV2(llm=mock_llm_high, knowledge=knowledge)
        result = await scorer_retry.score_single(sample_article)
        assert result["_fallback"] is False
        assert result["product_relevance"] == 88

    @pytest.mark.asyncio
    async def test_enrich_above_threshold(self):
        from agent.scorer_v2 import ScoringAgentV2

        validated = {"product_relevance": 50, "event_impact": 30, "score_reason": "ok", "tags": []}
        result = ScoringAgentV2._enrich_result(validated)
        assert result["pr_total_score"] == 80
        assert result["is_pr_candidate"] is True  # ≥80

    @pytest.mark.asyncio
    async def test_enrich_below_threshold(self):
        from agent.scorer_v2 import ScoringAgentV2

        validated = {"product_relevance": 30, "event_impact": 40, "score_reason": "low", "tags": []}
        result = ScoringAgentV2._enrich_result(validated)
        assert result["pr_total_score"] == 70
        assert result["is_pr_candidate"] is False  # <80

    @pytest.mark.asyncio
    async def test_fallback_score_structure(self):
        from agent.scorer_v2 import ScoringAgentV2

        result = ScoringAgentV2._fallback_score("test error")
        assert result["_fallback"] is True
        assert result["pr_total_score"] == 0
        assert "test error" in result["score_reason"]
        assert result["is_pr_candidate"] is False

    @pytest.mark.asyncio
    async def test_score_with_minimal_article(self, scorer):
        """最简文章也能正常打分"""
        result = await scorer.score_single(
            {"title": "T", "source": "S", "category_v2": "", "summary": ""}
        )
        assert result["product_relevance"] == 85


# ═══════════════════════════════════════════════════════════════
# 5. 阈值微调测试
# ═══════════════════════════════════════════════════════════════


class TestThresholdAdjustment:
    def test_calculate_threshold_increases_for_too_high_feedback(self):
        from agent.scorer_v2 import ScoringAgentV2

        feedbacks = [
            {"comment": "这篇文章打分偏高", "tags": []},
            {"comment": "", "tags": ["分数高"]},
        ]
        adjustment, directional_count = ScoringAgentV2.calculate_threshold_adjustment(feedbacks)
        assert adjustment == 4
        assert directional_count == 2

    def test_calculate_threshold_decreases_for_too_low_feedback(self):
        from agent.scorer_v2 import ScoringAgentV2

        feedbacks = [
            {"comment": "这个打分偏低，应该入选", "tags": []},
            {"comment": "", "tags": ["too_low"]},
        ]
        adjustment, directional_count = ScoringAgentV2.calculate_threshold_adjustment(feedbacks)
        assert adjustment == -4
        assert directional_count == 2

    def test_calculate_threshold_caps_to_ten_points(self):
        from agent.scorer_v2 import ScoringAgentV2

        feedbacks = [{"comment": "偏高", "tags": []} for _ in range(20)]
        adjustment, directional_count = ScoringAgentV2.calculate_threshold_adjustment(feedbacks)
        assert adjustment == 10
        assert directional_count == 20

    @pytest.mark.asyncio
    async def test_adjust_threshold_reads_feedbacks_from_db(self, scorer):
        class Cursor:
            async def to_list(self, length=None):
                return [
                    {"target_type": "article_score", "comment": "评分偏高", "tags": []},
                    {"target_type": "article_score", "comment": "", "tags": ["偏高"]},
                    {"target_type": "article_score", "comment": "不错", "tags": []},
                ]

        collection = MagicMock()
        collection.find.return_value = Cursor()
        db = {"feedbacks": collection}

        result = await scorer.adjust_threshold(db=db, user_id="local-user")
        assert result["threshold"] == 84
        assert result["adjustment"] == 4
        assert result["feedback_count"] == 3
        assert not hasattr(scorer, "pr_threshold")
        assert not hasattr(scorer, "threshold_adjustment")

    @pytest.mark.asyncio
    async def test_adjust_threshold_without_db_returns_default(self, scorer):
        result = await scorer.adjust_threshold(db=None, user_id="local-user")
        assert result["threshold"] == 80
        assert result["adjustment"] == 0
        assert not hasattr(scorer, "pr_threshold")

    @pytest.mark.asyncio
    async def test_adjust_threshold_query_failure_returns_default(self, scorer):
        class BrokenCollection:
            def find(self, query):
                raise RuntimeError("db unavailable")

        result = await scorer.adjust_threshold(
            db={"feedbacks": BrokenCollection()}, user_id="local-user"
        )

        assert result["threshold"] == 80
        assert result["adjustment"] == 0
        assert result["feedback_count"] == 0
        assert not hasattr(scorer, "pr_threshold")


# ═══════════════════════════════════════════════════════════════
# 6. 常量/配置测试
# ═══════════════════════════════════════════════════════════════


class TestConstants:
    """常量和配置验证"""

    def test_pr_threshold_is_80(self):
        from agent.scorer_v2 import PR_THRESHOLD

        assert PR_THRESHOLD == 80

    def test_score_bounds(self):
        from agent.scorer_v2 import SCORE_MAX, SCORE_MIN

        assert SCORE_MIN == 0
        assert SCORE_MAX == 100

    def test_default_temperature(self):
        from agent.scorer_v2 import DEFAULT_TEMPERATURE

        assert DEFAULT_TEMPERATURE == 0.1

    def test_max_retries(self):
        from agent.scorer_v2 import MAX_RETRIES

        assert MAX_RETRIES >= 1


# ═══════════════════════════════════════════════════════════════
# 7. 按产品注入知识测试
# ═══════════════════════════════════════════════════════════════


class TestProductKnowledgeInjection:
    @pytest.mark.asyncio
    async def test_single_product_prompt_injects_user_knowledge(self):
        """用户级产品评分时，系统提示词应注入该用户该产品的知识条目（而非全局硬编码文件）。"""
        from agent.scorer_v2 import ScoringAgentV2

        llm = MagicMock()
        llm.temperature = None
        knowledge = MagicMock()
        knowledge.as_scoring_prompt.return_value = "GLOBAL_KNOWLEDGE_PLACEHOLDER"

        user_product = {
            "product_id": "user-prod-1", "user_id": "u-1",
            "name": "星海外部攻击面管理平台", "enabled": True,
        }
        user_entry = {
            "entry_id": "entry-1", "user_id": "u-1",
            "product_id": "user-prod-1", "product_scope": "user",
            "doc_type": "overview", "title": "产品概述",
            "content": "该产品用于外部攻击面发现与管理，可对暴露面持续监测。",
            "enabled": True, "sort_order": 1,
        }

        user_products_col = MagicMock()
        user_products_col.find = MagicMock(
            return_value=MagicMock(to_list=AsyncMock(return_value=[user_product]))
        )

        def fake_find(query: dict):
            # 用户对全局产品的补充条目查询返回空
            if query.get("product_scope") == "global":
                return MagicMock(
                    sort=MagicMock(
                        return_value=MagicMock(to_list=AsyncMock(return_value=[]))
                    )
                )
            return MagicMock(
                sort=MagicMock(
                    return_value=MagicMock(to_list=AsyncMock(return_value=[user_entry]))
                )
            )

        entries_col = MagicMock()
        entries_col.find = MagicMock(side_effect=fake_find)

        db = {"user_products": user_products_col, "user_knowledge_entries": entries_col}
        scorer = ScoringAgentV2(llm=llm, knowledge=knowledge, db=db)

        prompt = await scorer._build_system_prompt_for_product(
            "user-prod-1", "星海外部攻击面管理平台", user_id="u-1"
        )

        assert "星海外部攻击面管理平台" in prompt
        assert "外部攻击面发现与管理" in prompt
        assert "用户级" in prompt
        # 用户级知识存在时不应回退到全局评分知识
        assert "GLOBAL_KNOWLEDGE_PLACEHOLDER" not in prompt

    @pytest.mark.asyncio
    async def test_single_product_prompt_falls_back_without_db(self):
        """无 DB 时回退到全局评分知识，不抛异常。"""
        from agent.scorer_v2 import ScoringAgentV2

        llm = MagicMock()
        llm.temperature = None
        knowledge = MagicMock()
        knowledge.as_scoring_prompt.return_value = "GLOBAL_KNOWLEDGE_PLACEHOLDER"
        scorer = ScoringAgentV2(llm=llm, knowledge=knowledge, db=None)

        prompt = await scorer._build_system_prompt_for_product(
            "user-prod-1", "星海外部攻击面管理平台", user_id="u-1"
        )

        assert "GLOBAL_KNOWLEDGE_PLACEHOLDER" in prompt
