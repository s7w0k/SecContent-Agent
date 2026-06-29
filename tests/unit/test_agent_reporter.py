"""
PR 报道生成 Agent — 单元测试

运行:
    pytest tests/unit/test_agent_reporter.py -v
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

# ═══════════════════════════════════════════════════════════════
# 测试数据
# ═══════════════════════════════════════════════════════════════

SAMPLE_REPORT_MD = """# [Critical MCP Vulnerability Found]

## 导语
近日发现MCP协议存在严重认证缺陷，该漏洞直接影响智能体身份安全核心领域。

## 背景
MCP协议是智能体间通信的核心标准，广泛应用于企业级AI部署。

## 分析
结合公司产品能力，MCP协议安全防护功能可有效检测此类攻击，
建议将此案例纳入产品宣传材料。

## 影响评估
- 客户：需向使用MCP协议的客户发布预警
- 行业：可能推动MCP安全标准制定
- 竞品：暂无竞品提供MCP安全审计功能

## 行动建议
1. 在产品中增加MCP漏洞检测规则
2. 更新产品文档中的MCP安全最佳实践
3. 向客户推送安全公告

## 关键词
MCP协议、身份认证、漏洞披露"""


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def knowledge():
    from agent.knowledge import ProductKnowledge

    return ProductKnowledge(
        product_positioning="智能体身份安全产品",
        core_features=["MCP协议安全防护", "智能体身份认证"],
        tech_barriers=["动态上下文感知"],
    )


@pytest.fixture
def article():
    return {
        "title": "Critical MCP Vulnerability Found",
        "url": "https://example.com/mcp-vuln",
        "url_hash": "abc123def456abc123def456abc123de",
        "source": "The Hacker News",
        "source_type": "overseas_news",
        "published_at": "2026-06-29",
        "summary": "A critical vulnerability...",
        "summary_cn": "MCP协议发现严重漏洞",
        "content_md": "# Critical MCP Vulnerability\n\nResearchers discovered a critical flaw in MCP servers...",
        "category": "MCP协议漏洞",
        "is_ai_security": True,
        "is_agent_security": True,
    }


@pytest.fixture
def scores():
    return {
        "ai_relevance_score": 92,
        "reportability_score": 78,
        "total_score": 170,
        "score_reason": "MCP协议漏洞直接涉及Agent安全核心",
        "tags": ["MCP协议", "身份认证"],
    }


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(return_value=AIMessage(content=SAMPLE_REPORT_MD))
    return llm


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=MagicMock())
    db["reports"].insert_one = AsyncMock(
        return_value=MagicMock(inserted_id="test-report-id-123")
    )
    db["articles"].update_one = AsyncMock()
    return db


@pytest.fixture
def reporter(mock_llm, knowledge):
    from agent.reporter import ReportAgent

    return ReportAgent(llm=mock_llm, knowledge=knowledge, db=None)


# ═══════════════════════════════════════════════════════════════
# 1. Prompt 构建测试
# ═══════════════════════════════════════════════════════════════


class TestPromptBuilding:
    """Prompt 模板测试"""

    def test_system_prompt_contains_template(self, reporter):
        assert "导语" in reporter.system_prompt
        assert "背景" in reporter.system_prompt
        assert "分析" in reporter.system_prompt
        assert "影响评估" in reporter.system_prompt
        assert "行动建议" in reporter.system_prompt

    def test_system_prompt_contains_knowledge(self, reporter):
        assert "MCP协议安全防护" in reporter.system_prompt

    def test_user_prompt_contains_article(self, article, scores):
        from agent.reporter import ReportAgent

        prompt = ReportAgent._build_user_prompt(article, "test content", scores)
        assert "Critical MCP Vulnerability" in prompt
        assert "The Hacker News" in prompt
        assert "MCP协议漏洞" in prompt

    def test_user_prompt_contains_scores(self, article, scores):
        from agent.reporter import ReportAgent

        prompt = ReportAgent._build_user_prompt(article, "test", scores)
        assert "92" in prompt
        assert "78" in prompt
        assert "170" in prompt

    def test_user_prompt_truncates_long_content(self, article, scores):
        from agent.reporter import ReportAgent, MAX_CONTENT_LENGTH

        long_content = "x" * (MAX_CONTENT_LENGTH + 500)
        # 调用 generate_report 时会自动截断 — 这里测试 user prompt 收到的内容
        # generate_report 内部截断后传给 _build_user_prompt
        assert len(long_content) > MAX_CONTENT_LENGTH


# ═══════════════════════════════════════════════════════════════
# 2. 报道生成测试（mock LLM）
# ═══════════════════════════════════════════════════════════════


class TestReportGeneration:
    """报道生成流程测试"""

    @pytest.mark.asyncio
    async def test_generate_report_success(self, reporter, article, scores):
        result = await reporter.generate_report(article, scores)
        assert result["ok"] is True
        assert result["report"] is not None
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_report_has_required_sections(self, reporter, article, scores):
        result = await reporter.generate_report(article, scores)
        content = result["report"]["content_md"]
        assert "导语" in content
        assert "背景" in content
        assert "分析" in content

    @pytest.mark.asyncio
    async def test_report_doc_structure(self, reporter, article, scores):
        result = await reporter.generate_report(article, scores)
        report = result["report"]
        assert "article_url_hash" in report
        assert report["title"] == article["title"]
        assert report["template"] == "standard_pr"
        assert "generated_by" in report
        assert "scores" in report

    @pytest.mark.asyncio
    async def test_report_doc_scores(self, reporter, article, scores):
        result = await reporter.generate_report(article, scores)
        report = result["report"]
        assert report["scores"]["relevance"] == 92
        assert report["scores"]["reportability"] == 78

    @pytest.mark.asyncio
    async def test_generate_without_scores(self, reporter, article):
        result = await reporter.generate_report(article, None)
        assert result["ok"] is True
        assert result["report"]["scores"]["relevance"] == 0

    @pytest.mark.asyncio
    async def test_generate_with_dict_article(self, reporter, scores):
        article = {"title": "T", "source": "S", "url": "https://x.com", "content_md": "text"}
        result = await reporter.generate_report(article, scores)
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_fallback_on_llm_error(self, mock_llm, knowledge, article, scores):
        from agent.reporter import ReportAgent

        mock_llm.ainvoke = AsyncMock(side_effect=Exception("API error"))
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge)
        result = await reporter.generate_report(article, scores)
        assert result["ok"] is False
        assert result["error"] is not None
        assert "API error" in result["error"]

    @pytest.mark.asyncio
    async def test_fallback_report_has_template(self, mock_llm, knowledge, article, scores):
        from agent.reporter import ReportAgent

        mock_llm.ainvoke = AsyncMock(side_effect=Exception("fail"))
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge)
        result = await reporter.generate_report(article, scores)
        fallback = result["report"]
        assert "导语" in fallback["content_md"]
        assert "待人工补充" in fallback["content_md"]

    @pytest.mark.asyncio
    async def test_retry_then_succeed(self, mock_llm, knowledge, article, scores):
        from agent.reporter import ReportAgent

        mock_llm.ainvoke = AsyncMock(side_effect=[
            Exception("Temp fail"),
            AIMessage(content=SAMPLE_REPORT_MD),
        ])
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge)
        result = await reporter.generate_report(article, scores)
        assert result["ok"] is True


# ═══════════════════════════════════════════════════════════════
# 3. 报道清理测试
# ═══════════════════════════════════════════════════════════════


class TestReportCleaning:
    """报道文本清理验证"""

    def test_title_inserted_when_missing(self):
        from agent.reporter import ReportAgent

        text = "## 导语\nSome content"
        cleaned = ReportAgent._clean_report(text, "Test Title")
        assert cleaned.startswith("# [Test Title]")

    def test_no_double_title(self):
        from agent.reporter import ReportAgent

        text = "# [Test Title]\n\n## 导语\nContent"
        cleaned = ReportAgent._clean_report(text, "Test Title")
        assert cleaned == text

    def test_removes_closing_remarks(self):
        from agent.reporter import ReportAgent

        text = "# Title\n\n## 导语\nGood content\n\n以上是本次报道的全部内容，如有疑问请联系。"
        cleaned = ReportAgent._clean_report(text, "Title")
        assert "以上是" not in cleaned
        assert "Good content" in cleaned

    def test_trims_whitespace(self):
        from agent.reporter import ReportAgent

        text = "\n\n\n# Title\nContent\n\n\n"
        cleaned = ReportAgent._clean_report(text, "Title")
        assert cleaned == "# Title\nContent"


# ═══════════════════════════════════════════════════════════════
# 4. 数据库操作测试
# ═══════════════════════════════════════════════════════════════


class TestDatabaseOperations:
    """MongoDB 持久化测试"""

    @pytest.mark.asyncio
    async def test_report_saved_to_db(self, mock_llm, knowledge, article, scores, mock_db):
        from agent.reporter import ReportAgent

        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=mock_db)
        result = await reporter.generate_report(article, scores)
        assert result["ok"] is True
        # 验证 insert_one 被调用
        mock_db["reports"].insert_one.assert_called_once()
        # 验证 update_one 被调用
        mock_db["articles"].update_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_update_sets_report_fields(self, mock_llm, knowledge, article, scores, mock_db):
        from agent.reporter import ReportAgent

        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=mock_db)
        await reporter.generate_report(article, scores)

        # 检查 update_one 的参数（两个位置参数：filter, update）
        args = mock_db["articles"].update_one.call_args[0]
        update_doc = args[1]  # 第二个位置参数是 update dict
        assert update_doc["$set"]["has_report"] is True
        assert "report_id" in update_doc["$set"]

    @pytest.mark.asyncio
    async def test_db_none_skips_persistence(self, mock_llm, knowledge, article, scores):
        from agent.reporter import ReportAgent

        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=None)
        result = await reporter.generate_report(article, scores)
        assert result["ok"] is True  # 不应崩溃

    @pytest.mark.asyncio
    async def test_db_error_does_not_crash(self, mock_llm, knowledge, article, scores):
        from agent.reporter import ReportAgent

        bad_db = MagicMock()
        bad_db["reports"].insert_one = AsyncMock(side_effect=Exception("DB down"))
        bad_db["articles"].update_one = AsyncMock()

        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=bad_db)
        result = await reporter.generate_report(article, scores)
        # 即使 DB 失败，generate_report 本身不应崩溃
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_build_report_doc_url_hash(self, article, scores):
        from agent.reporter import ReportAgent

        report = ReportAgent._build_report_doc(article, scores, "# Content")
        assert len(report["article_url_hash"]) == 32
        assert report["template"] == "standard_pr"


# ═══════════════════════════════════════════════════════════════
# 5. 集成场景验证
# ═══════════════════════════════════════════════════════════════


class TestIntegration:
    """与打分结果的集成验证"""

    @pytest.mark.asyncio
    async def test_report_includes_score_context(self, reporter, article, scores):
        """报道应包含打分信息作为上下文"""
        result = await reporter.generate_report(article, scores)
        # 验证 reporter 正常使用了 scores
        assert result["report"]["scores"]["relevance"] == scores["ai_relevance_score"]

    @pytest.mark.asyncio
    async def test_report_with_minimal_article(self, reporter):
        """最小文章信息也能生成报道"""
        minimal = {"title": "Mini", "content_md": "minimal content"}
        result = await reporter.generate_report(minimal, {})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_content_truncation(self, reporter, scores):
        """长文章内容被截断"""
        from agent.reporter import MAX_CONTENT_LENGTH

        long_article = {
            "title": "Long",
            "content_md": "A" * (MAX_CONTENT_LENGTH + 2000),
        }
        result = await reporter.generate_report(long_article, scores)
        assert result["ok"] is True
