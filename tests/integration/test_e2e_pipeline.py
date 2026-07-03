"""
端到端流水线集成测试

使用 mock 外部依赖（MCP Bridge / LLM）验证全链路：
  crawl → classify → score → report

运行:
    pytest tests/integration/test_e2e_pipeline.py -v
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

MOCK_ARTICLES = [
    {
        "title": "Critical MCP Protocol Vulnerability Discovered",
        "url": "https://example.com/mcp-vuln",
        "url_hash": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
        "source": "The Hacker News",
        "source_type": "overseas_news",
        "summary": "Researchers found a critical authentication bypass in MCP servers.",
        "content_md": "# Critical MCP Vulnerability\n\nFull details...",
        "published_at": "2026-06-29",
    },
    {
        "title": "New AI Agent Framework Released",
        "url": "https://example.com/agent-framework",
        "url_hash": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
        "source": "SecurityWeek",
        "source_type": "overseas_news",
        "summary": "A new framework for building secure AI agents has been released.",
        "content_md": "# New AI Agent Framework\n\nAnnouncement...",
        "published_at": "2026-06-28",
    },
]

MOCK_CLASSIFIED = [
    {**MOCK_ARTICLES[0], "is_ai_security": True, "is_agent_security": True, "category": "MCP协议漏洞", "summary_cn": "MCP漏洞"},
    {**MOCK_ARTICLES[1], "is_ai_security": True, "is_agent_security": False, "category": "AI安全", "summary_cn": "AI框架"},
]

SCORE_RESPONSE = AIMessage(content=json.dumps({
    "ai_relevance_score": 92,
    "reportability_score": 78,
    "score_reason": "MCP协议认证缺陷直接涉及Agent安全核心",
    "tags": ["MCP协议", "身份认证"],
}))

SCORE_RESPONSE_LOW = AIMessage(content=json.dumps({
    "ai_relevance_score": 40,
    "reportability_score": 30,
    "score_reason": "一般性框架发布，非安全事件",
    "tags": ["AI框架"],
}))

REPORT_RESPONSE = AIMessage(content="""# [Critical MCP Protocol Vulnerability Discovered]

## 导语
近日发现MCP协议存在严重认证缺陷，直接影响智能体身份安全核心领域。

## 背景
MCP协议是智能体间通信的核心标准，广泛应用于企业级AI部署。

## 分析
结合公司MCP协议安全防护产品能力，该漏洞可通过动态上下文感知引擎检测。

## 影响评估
- 客户：需向MCP协议用户发布安全预警
- 行业：可能推动MCP安全标准制定
- 竞品：暂无竞品提供完整MCP安全审计方案

## 行动建议
1. 更新产品MCP漏洞检测规则
2. 向客户推送安全公告
3. 将案例纳入产品白皮书

## 关键词
MCP协议、身份认证、漏洞披露""")


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def knowledge():
    from agent.knowledge import ProductKnowledge

    return ProductKnowledge(
        product_positioning="智能体身份安全产品 — 定义AI时代的安全身份通行证",
        core_features=["MCP协议安全防护", "智能体身份认证", "意图识别"],
        tech_barriers=["动态上下文感知引擎"],
        control_points=["首家MCP安全审计", "运营商级验证"],
        customer_cases=["北京移动：家宽智能体身份防护"],
    )


@pytest.fixture
def mock_tools():
    """Mock MCP 工具集 — 模拟 HTTP Bridge 返回数据"""
    tools = {}

    # crawl_overseas_news
    crawl_tool = MagicMock()
    crawl_tool.ainvoke = AsyncMock(return_value={
        "ok": True,
        "data": {"articles": MOCK_ARTICLES, "count": len(MOCK_ARTICLES)},
    })
    tools["crawl_overseas_news"] = crawl_tool

    # fetch_wewe_articles (empty)
    wewe_tool = MagicMock()
    wewe_tool.ainvoke = AsyncMock(return_value={"ok": True, "data": []})
    tools["fetch_wewe_articles"] = wewe_tool

    # classify_articles
    classify_tool = MagicMock()
    classify_tool.ainvoke = AsyncMock(return_value={
        "ok": True,
        "data": {"classified": MOCK_CLASSIFIED},
    })
    tools["classify_articles"] = classify_tool

    # Other tools (not used in pipeline but required by create_mcp_toolset)
    for name in ["query_articles", "get_crawl_stats", "export_articles_csv",
                 "fetch_article_fulltext", "analyze_wewe_article"]:
        t = MagicMock()
        t.ainvoke = AsyncMock(return_value={"ok": True, "data": {}})
        tools[name] = t

    return tools


@pytest.fixture
def mock_llm():
    """Mock LLM — 根据输入返回不同的打分/报道结果"""
    llm = MagicMock()

    call_count = [0]

    async def _ainvoke(messages):
        call_count[0] += 1
        # First call → high score, second → low score
        if call_count[0] <= 1:
            return SCORE_RESPONSE
        elif call_count[0] <= 2:
            return SCORE_RESPONSE_LOW
        else:
            return REPORT_RESPONSE

    llm.ainvoke = _ainvoke
    return llm


@pytest.fixture
def mock_db():
    """Mock MongoDB — 内存存储模拟"""
    db = MagicMock()
    articles_store: list[dict] = []
    reports_store: list[dict] = []

    articles_mock = MagicMock()
    reports_mock = MagicMock()

    def _getitem(key):
        return articles_mock if key == "articles" else reports_mock

    db.__getitem__.side_effect = _getitem

    # articles ops
    async def _insert_one(doc):
        doc["_id"] = f"art_{len(articles_store)}"
        articles_store.append(doc)
        return MagicMock(inserted_id=doc["_id"])

    async def _find_one(query):
        for a in articles_store:
            if a.get("url_hash") == query.get("url_hash"):
                return a
        return None

    def _find(query=None):
        mock_cursor = MagicMock()

        async def _to_list(length=100):
            if query is None:
                return list(articles_store)
            results = []
            for a in articles_store:
                match = True
                for k, v in query.items():
                    if k == "pipeline_status":
                        if a.get("pipeline_status") != v:
                            match = False
                    elif k == "is_ai_security" and a.get("is_ai_security") != v:
                        match = False
                if match:
                    results.append(dict(a))
            return results

        mock_cursor.to_list = _to_list
        mock_cursor.sort = MagicMock(return_value=mock_cursor)
        mock_cursor.skip = MagicMock(return_value=mock_cursor)
        mock_cursor.limit = MagicMock(return_value=mock_cursor)
        return mock_cursor

    async def _update_one(filter_dict, update_dict):
        for a in articles_store:
            match = True
            for k, v in filter_dict.items():
                if k == "_id":
                    if a.get("_id") != v:
                        match = False
                elif a.get(k) != v:
                    match = False
            if match:
                if "$set" in update_dict:
                    a.update(update_dict["$set"])
                return MagicMock(modified_count=1)
        return MagicMock(modified_count=0)

    async def _count_docs(query=None):
        if query is None:
            return len(articles_store)
        return len(articles_store)

    async def _aggregate(pipeline):
        mock_agg = MagicMock()
        mock_agg.__aiter__.return_value = iter([])
        return mock_agg

    articles_mock.insert_one = _insert_one
    articles_mock.find_one = _find_one
    articles_mock.find = _find
    articles_mock.update_one = _update_one
    articles_mock.count_documents = _count_docs
    articles_mock.aggregate = _aggregate

    # reports ops
    async def _insert_report(doc):
        doc["_id"] = f"rpt_{len(reports_store)}"
        reports_store.append(doc)
        return MagicMock(inserted_id=doc["_id"])

    reports_mock.insert_one = _insert_report
    reports_mock.find_one = AsyncMock(return_value=None)
    reports_mock.count_documents = AsyncMock(return_value=0)
    mock_rpt_cursor = MagicMock()
    mock_rpt_cursor.to_list = AsyncMock(return_value=[])
    reports_mock.find = MagicMock(return_value=mock_rpt_cursor)

    # 暴露存储用于断言
    db._articles = articles_store
    db._reports = reports_store

    return db


# ═══════════════════════════════════════════════════════════════
# 1. 完整流水线集成测试
# ═══════════════════════════════════════════════════════════════


class TestFullPipeline:
    """全流程端到端测试（crawl → classify → score → report）"""

    @pytest.mark.asyncio
    async def test_full_pipeline_with_mock_data(self, mock_tools, mock_llm, knowledge, mock_db):
        """使用 mock 数据运行完整流水线"""
        from agent.pipeline import PipelineManager
        from agent.reporter import ReportAgent
        from agent.scorer import ScoringAgent

        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=mock_db)

        manager = PipelineManager(
            tools=mock_tools,
            scorer=scorer,
            reporter=reporter,
            knowledge=MagicMock(load=AsyncMock(), as_system_prompt=MagicMock(return_value="test")),
            db=mock_db,
        )

        result = await manager.run_full(crawl_days=1)
        state = result["state"]

        assert result["status"] == "completed"
        assert state["crawled_count"] >= 1
        assert state["classified_count"] >= 1
        assert state["scored_count"] >= 1
        assert state["report_count"] >= 1

        # 验证数据库中的文章
        assert len(mock_db._articles) >= 1
        # 验证报道已生成
        assert len(mock_db._reports) >= 1

    @pytest.mark.asyncio
    async def test_articles_flow_through_statuses(self, mock_tools, mock_llm, knowledge, mock_db):
        """验证文章状态流转：crawled → classified → scored"""
        from agent.pipeline import PipelineManager
        from agent.reporter import ReportAgent
        from agent.scorer import ScoringAgent

        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=mock_db)

        manager = PipelineManager(mock_tools, scorer, reporter, MagicMock(load=AsyncMock()), mock_db)
        await manager.run_full(crawl_days=1)

        # 检查 pipeline_status 字段
        statuses = [a.get("pipeline_status") for a in mock_db._articles]
        assert "scored" in statuses or "classified" in statuses or "crawled" in statuses

    @pytest.mark.asyncio
    async def test_high_value_article_generates_report(self, mock_tools, mock_llm, knowledge, mock_db):
        """高分文章（≥140）应生成报道"""
        from agent.pipeline import PipelineManager
        from agent.reporter import ReportAgent
        from agent.scorer import ScoringAgent

        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=mock_db)

        manager = PipelineManager(mock_tools, scorer, reporter, MagicMock(load=AsyncMock()), mock_db)
        result = await manager.run_full(crawl_days=1)

        # 至少生成一篇报道（第一批文章分数 92+78=170≥140）
        assert result["state"]["report_count"] >= 1

    @pytest.mark.asyncio
    async def test_pipeline_status_tracking(self, mock_tools, mock_llm, knowledge, mock_db):
        """验证流水线状态在各阶段正确更新"""
        from agent.pipeline import PipelineManager
        from agent.reporter import ReportAgent
        from agent.scorer import ScoringAgent

        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=mock_db)

        manager = PipelineManager(mock_tools, scorer, reporter, MagicMock(load=AsyncMock()), mock_db)

        # 初始状态 idle
        assert manager.get_status()["status"] == "idle"

        # 运行后 completed
        await manager.run_full()
        status = manager.get_status()
        assert status["status"] == "completed"
        assert len(status["state"]["errors"]) == 0


# ═══════════════════════════════════════════════════════════════
# 2. 错误恢复测试
# ═══════════════════════════════════════════════════════════════


class TestErrorRecovery:
    """异常场景恢复验证"""

    @pytest.mark.asyncio
    async def test_crawl_failure_does_not_block_pipeline(self, mock_tools, mock_llm, knowledge, mock_db):
        """爬取失败不应阻塞分类/打分/报道阶段"""
        from agent.pipeline import PipelineManager
        from agent.reporter import ReportAgent
        from agent.scorer import ScoringAgent

        # 让 crawl tool 抛出异常
        mock_tools["crawl_overseas_news"].ainvoke = AsyncMock(
            side_effect=Exception("Crawl service unavailable")
        )

        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=mock_db)

        manager = PipelineManager(mock_tools, scorer, reporter, MagicMock(load=AsyncMock()), mock_db)
        result = await manager.run_full(crawl_days=1)

        # 应标记为 completed（非阻塞），但有错误记录
        assert result["status"] == "completed"
        assert len(result["state"]["errors"]) >= 1

    @pytest.mark.asyncio
    async def test_phase_by_phase_execution(self, mock_tools, mock_llm, knowledge, mock_db):
        """逐阶段执行 — 验证阶段独立性"""
        from agent.pipeline import PipelineManager
        from agent.reporter import ReportAgent
        from agent.scorer import ScoringAgent

        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=mock_db)

        manager = PipelineManager(mock_tools, scorer, reporter, MagicMock(load=AsyncMock()), mock_db)

        # 阶段 1: 仅爬取
        r1 = await manager.run_phase("crawl")
        assert r1["status"] == "completed"
        assert r1["state"]["crawled_count"] >= 1

        # 阶段 2: 分类（包含 crawl 前置 → 但 articles 已存在会被跳过）
        r2 = await manager.run_phase("classify")
        assert r2["status"] == "completed"

        # 阶段 3: 打分
        r3 = await manager.run_phase("score")
        assert r3["status"] == "completed"

        # 阶段 4: 报道（全程跑通，因为前面的数据已入库）
        r4 = await manager.run_phase("report")
        assert r4["status"] == "completed"
        assert r4["state"]["report_count"] >= 1

    @pytest.mark.asyncio
    async def test_empty_crawl_handled_gracefully(self, mock_tools, mock_llm, knowledge, mock_db):
        """空爬取结果不应崩溃"""
        from agent.pipeline import PipelineManager
        from agent.reporter import ReportAgent
        from agent.scorer import ScoringAgent

        # 返回空数据
        mock_tools["crawl_overseas_news"].ainvoke = AsyncMock(return_value={
            "ok": True, "data": {"articles": [], "count": 0},
        })

        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge, db=mock_db)

        manager = PipelineManager(mock_tools, scorer, reporter, MagicMock(load=AsyncMock()), mock_db)
        result = await manager.run_full(crawl_days=1)

        assert result["status"] == "completed"
        assert result["state"]["crawled_count"] == 0


# ═══════════════════════════════════════════════════════════════
# 3. 工具集成验证
# ═══════════════════════════════════════════════════════════════


class TestToolIntegration:
    """MCP 工具与流水线集成"""

    def test_toolset_creation(self):
        """工具集创建后包含全部 8 个工具"""
        from agent.tools import create_mcp_toolset

        tools = create_mcp_toolset(
            wewe_url="http://test:8100",
            crawl_url="http://test:8101",
        )
        assert len(tools) == 8
        assert "crawl_overseas_news" in tools
        assert "classify_articles" in tools
        assert "fetch_wewe_articles" in tools

    @pytest.mark.asyncio
    async def test_tool_http_call_mocked(self):
        """Tool → HTTP 调用可以 mock"""
        from agent.tools import create_mcp_toolset

        with patch("agent.tools.httpx.AsyncClient") as mock_cls:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"ok": True, "data": {"total": 42}}
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_client

            tools = create_mcp_toolset(crawl_url="http://test:8101")
            result = await tools["get_crawl_stats"].ainvoke({"payload": {}})
            assert result["ok"] is True
            assert result["data"]["total"] == 42


# ═══════════════════════════════════════════════════════════════
# 4. 打分-报道联动测试
# ═══════════════════════════════════════════════════════════════


class TestScorerReporterIntegration:
    """打分结果正确传递到报道生成"""

    @pytest.mark.asyncio
    async def test_score_data_flows_to_reporter(self, mock_llm, knowledge):
        """验证打分结果作为报道生成输入"""
        from agent.reporter import ReportAgent
        from agent.scorer import ScoringAgent

        scorer = ScoringAgent(llm=mock_llm, knowledge=knowledge)
        reporter = ReportAgent(llm=mock_llm, knowledge=knowledge)

        article = {
            "title": "MCP Vuln",
            "url": "https://x.com",
            "url_hash": "abc123",
            "source": "THN",
            "summary": "Critical flaw",
            "content_md": "# Content",
            "category": "MCP协议漏洞",
        }

        # 打分
        scores = await scorer.score_single(article)
        assert scores["total_score"] == 170  # 92+78

        # 传递分数到报道
        result = await reporter.generate_report(article, scores)
        assert result["ok"] is True
        assert result["report"]["scores"]["relevance"] == 92
