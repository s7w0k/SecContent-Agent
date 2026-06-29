"""
Agent 流水线编排 — LangGraph StateGraph

实现 crawl → classify → score → report 四阶段流水线，
通过 LangGraph 状态图编排，支持全流程和单阶段执行。

特性:
  - 4 节点状态图（crawl / classify / score / report）
  - 每阶段独立可触发（支持断点续跑）
  - PipelineManager 管理生命周期（run / status / cancel）
  - 错误隔离：某阶段失败不影响后续独立执行的阶段

使用:
    from agent.pipeline import PipelineManager

    manager = PipelineManager(tools, scorer, reporter, knowledge, db)
    result = await manager.run_full(crawl_days=1)
    status = manager.get_status()
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Callable, Optional

from langgraph.graph import StateGraph, END

logger = logging.getLogger("backend.agent.pipeline")


# ═══════════════════════════════════════════════════════════════
# 状态定义
# ═══════════════════════════════════════════════════════════════


class PipelinePhase(str, Enum):
    CRAWL = "crawl"
    CLASSIFY = "classify"
    SCORE = "score"
    REPORT = "report"


class PipelineStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def create_state(
    crawl_days: int = 1,
    phases: Optional[list[str]] = None,
) -> dict:
    """创建流水线初始状态（普通 dict，兼容 LangGraph）"""
    return {
        "crawl_days": crawl_days,
        "phases": phases or [p.value for p in PipelinePhase],
        "crawled_count": 0,
        "classified_count": 0,
        "scored_count": 0,
        "report_count": 0,
        "errors": [],
        "status": PipelineStatus.IDLE.value,
        "current_phase": "",
        "started_at": "",
        "finished_at": "",
    }


# ═══════════════════════════════════════════════════════════════
# 流水线节点
# ═══════════════════════════════════════════════════════════════


async def crawl_node(state: dict, tools: dict, db: Any) -> dict:
    """爬取阶段：调用 mcp-wewe + mcp-crawl 获取文章，存入 MongoDB。

    Args:
        state: 流水线状态
        tools: MCP Tool 字典（来自 tools.create_mcp_toolset）
        db: MongoDB 数据库实例
    """
    state["current_phase"] = PipelinePhase.CRAWL.value
    logger.info("[crawl] Starting crawl phase (days=%d)", state["crawl_days"])

    if PipelinePhase.CRAWL.value not in state["phases"]:
        logger.info("[crawl] Skipped (not in selected phases)")
        return state

    try:
        articles: list[dict] = []

        # 1. 爬取海外安全新闻
        crawl_result = await tools["crawl_overseas_news"].ainvoke(
            {"payload": {"days": state["crawl_days"]}}
        )
        if crawl_result.get("ok") and crawl_result.get("data"):
            data = crawl_result["data"]
            if isinstance(data, dict) and "articles" in data:
                articles.extend(data["articles"])

        # 2. 获取微信公众号文章
        try:
            wewe_result = await tools["fetch_wewe_articles"].ainvoke({"payload": {}})
            if wewe_result.get("ok") and wewe_result.get("data"):
                data = wewe_result["data"]
                wewe_articles = data if isinstance(data, list) else data.get("articles", [])
                articles.extend(wewe_articles)
        except Exception as e:
            logger.warning("[crawl] WeWe fetch failed (non-critical): %s", e)

        # 3. 去重 + 入库
        if db is not None and articles:
            saved_count = 0
            for art in articles:
                try:
                    url_hash = art.get("url_hash", "")
                    if not url_hash:
                        import hashlib
                        url_hash = hashlib.md5(
                            art.get("url", "").encode()
                        ).hexdigest()

                    # Upsert: 已存在的跳过
                    existing = await db["articles"].find_one({"url_hash": url_hash})
                    if existing:
                        continue

                    doc = {
                        "url_hash": url_hash,
                        "title": art.get("title", ""),
                        "url": art.get("url", ""),
                        "source": art.get("source", ""),
                        "source_type": art.get("source_type", "overseas_news"),
                        "published_at": art.get("published_at", ""),
                        "summary": art.get("summary", ""),
                        "summary_cn": art.get("summary_cn", ""),
                        "content_md": art.get("content_md", ""),
                        "is_ai_security": art.get("is_ai_security", False),
                        "is_agent_security": art.get("is_agent_security", False),
                        "category": art.get("category", ""),
                        "ai_relevance_score": 0,
                        "reportability_score": 0,
                        "score_reason": "",
                        "has_report": False,
                        "report_id": None,
                        "added_at": _now_iso(),
                        "pipeline_status": "crawled",
                    }
                    await db["articles"].insert_one(doc)
                    saved_count += 1
                except Exception as e:
                    logger.warning("[crawl] Failed to save article: %s", e)

            state["crawled_count"] = saved_count
            logger.info("[crawl] Saved %d new articles (%d total crawled, %d duplicates)",
                        saved_count, len(articles), len(articles) - saved_count)
        else:
            state["crawled_count"] = len(articles)
            logger.info("[crawl] %d articles crawled (no DB, not saved)", len(articles))

    except Exception as e:
        logger.error("[crawl] Phase failed: %s", e)
        state["errors"].append(f"crawl: {e}")

    return state


async def classify_node(state: dict, tools: dict, db: Any) -> dict:
    """分类阶段：对已爬取文章进行 AI 分类。

    从 MongoDB 读取 pipeline_status="crawled" 的文章，调用 mcp-crawl 分类。
    """
    state["current_phase"] = PipelinePhase.CLASSIFY.value
    logger.info("[classify] Starting classify phase")

    if PipelinePhase.CLASSIFY.value not in state["phases"]:
        logger.info("[classify] Skipped (not in selected phases)")
        return state

    try:
        if db is None:
            logger.info("[classify] No DB, skipping")
            return state

        # 读取待分类文章
        cursor = db["articles"].find({"pipeline_status": "crawled"})
        raw_articles = await cursor.to_list(length=100)

        if not raw_articles:
            logger.info("[classify] No articles to classify")
            return state

        # 转换为分类请求格式
        import json as _json
        articles_json = _json.dumps([
            {
                "title": a.get("title", ""),
                "url": a.get("url", ""),
                "source": a.get("source", ""),
                "summary": a.get("summary", ""),
            }
            for a in raw_articles
        ], ensure_ascii=False)

        classify_result = await tools["classify_articles"].ainvoke(
            {"payload": {"articles_json": articles_json}}
        )

        if classify_result.get("ok") and classify_result.get("data"):
            data = classify_result["data"]
            classified = data.get("classified", []) if isinstance(data, dict) else data

            for i, art in enumerate(raw_articles):
                if i < len(classified):
                    cls = classified[i]
                    await db["articles"].update_one(
                        {"_id": art["_id"]},
                        {"$set": {
                            "is_ai_security": cls.get("is_ai_security", False),
                            "is_agent_security": cls.get("is_agent_security", False),
                            "category": cls.get("category", ""),
                            "summary_cn": cls.get("summary_cn", ""),
                            "pipeline_status": "classified",
                        }},
                    )

            state["classified_count"] = len(raw_articles)
            logger.info("[classify] Classified %d articles", len(raw_articles))

    except Exception as e:
        logger.error("[classify] Phase failed: %s", e)
        state["errors"].append(f"classify: {e}")

    return state


async def score_node(
    state: dict,
    tools: dict,
    scorer: Any,
    knowledge: Any,
    db: Any,
) -> dict:
    """打分阶段：对已分类的 AI 安全文章进行双维度打分。

    从 MongoDB 读取 pipeline_status="classified" 且 is_ai_security=true 的文章。
    """
    state["current_phase"] = PipelinePhase.SCORE.value
    logger.info("[score] Starting score phase")

    if PipelinePhase.SCORE.value not in state["phases"]:
        logger.info("[score] Skipped (not in selected phases)")
        return state

    try:
        if db is None:
            logger.info("[score] No DB, skipping")
            return state

        # 确保知识库已加载
        await knowledge.load()

        # 读取待打分文章
        cursor = db["articles"].find({
            "pipeline_status": "classified",
            "is_ai_security": True,
        })
        raw_articles = await cursor.to_list(length=50)

        if not raw_articles:
            logger.info("[score] No articles to score")
            return state

        # 批量打分
        scored = await scorer.score_batch(raw_articles)
        scored_count = 0

        for art, scores in zip(raw_articles, scored):
            if not scores.get("_fallback", True):
                await db["articles"].update_one(
                    {"_id": art["_id"]},
                    {"$set": {
                        "ai_relevance_score": scores["ai_relevance_score"],
                        "reportability_score": scores["reportability_score"],
                        "score_reason": scores.get("score_reason", ""),
                        "pipeline_status": "scored",
                    }},
                )
                scored_count += 1

        state["scored_count"] = scored_count
        logger.info("[score] Scored %d/%d articles", scored_count, len(raw_articles))

    except Exception as e:
        logger.error("[score] Phase failed: %s", e)
        state["errors"].append(f"score: {e}")

    return state


async def report_node(
    state: dict,
    tools: dict,
    reporter: Any,
    knowledge: Any,
    db: Any,
) -> dict:
    """报道生成阶段：对高分文章生成 PR 报道。

    从 MongoDB 读取 pipeline_status="scored" 且 total_score≥140 的文章。
    """
    state["current_phase"] = PipelinePhase.REPORT.value
    logger.info("[report] Starting report generation phase")

    if PipelinePhase.REPORT.value not in state["phases"]:
        logger.info("[report] Skipped (not in selected phases)")
        return state

    try:
        if db is None:
            logger.info("[report] No DB, skipping")
            return state

        await knowledge.load()

        # 读取高分文章（pipeline 内计算 total_score）
        cursor = db["articles"].find({"pipeline_status": "scored"})
        raw_articles = await cursor.to_list(length=30)

        # 筛选 total_score ≥ 140
        high_value: list[dict] = []
        for a in raw_articles:
            total = a.get("ai_relevance_score", 0) + a.get("reportability_score", 0)
            if total >= 140:
                a["_total_score"] = total
                high_value.append(a)

        if not high_value:
            logger.info("[report] No high-value articles (≥140)")
            return state

        report_count = 0
        for art in high_value:
            scores = {
                "ai_relevance_score": art.get("ai_relevance_score", 0),
                "reportability_score": art.get("reportability_score", 0),
                "total_score": art.get("_total_score", 0),
                "score_reason": art.get("score_reason", ""),
            }
            result = await reporter.generate_report(art, scores)
            if result["ok"]:
                report_count += 1

        state["report_count"] = report_count
        logger.info("[report] Generated %d reports", report_count)

    except Exception as e:
        logger.error("[report] Phase failed: %s", e)
        state["errors"].append(f"report: {e}")

    return state


# ═══════════════════════════════════════════════════════════════
# PipelineManager
# ═══════════════════════════════════════════════════════════════


class PipelineManager:
    """流水线生命周期管理器。

    管理 LangGraph 流水线的执行、状态查询和取消。
    """

    def __init__(
        self,
        tools: dict[str, Callable],
        scorer: Any,         # ScoringAgent
        reporter: Any,       # ReportAgent
        knowledge: Any,      # KnowledgeLoader
        db: Any = None,      # AsyncIOMotorDatabase
    ):
        self.tools = tools
        self.scorer = scorer
        self.reporter = reporter
        self.knowledge = knowledge
        self.db = db

        # 状态
        self._state: Optional[dict] = None
        self._task: Optional[asyncio.Task] = None
        self._graph = self._build_graph()

    # ── 公开接口 ──────────────────────────────────────────────

    async def run_full(self, crawl_days: int = 1) -> dict:
        """执行全流程流水线。

        Args:
            crawl_days: 爬取天数

        Returns:
            {"pipeline_id": str, "status": str, "state": dict}
        """
        phases = [p.value for p in PipelinePhase]
        return await self._run(crawl_days, phases)

    async def run_phase(
        self,
        phase: str,
        crawl_days: int = 1,
    ) -> dict:
        """执行单个阶段（及其前置依赖）。

        Args:
            phase: 阶段名称 (crawl | classify | score | report)
            crawl_days: 爬取天数（仅 crawl 阶段使用）

        Returns:
            {"pipeline_id": str, "status": str, "state": dict}
        """
        phase_order = [p.value for p in PipelinePhase]
        if phase not in phase_order:
            raise ValueError(f"Unknown phase: {phase}. Valid: {phase_order}")

        idx = phase_order.index(phase)
        phases = phase_order[: idx + 1]  # 包含该阶段及之前所有阶段
        return await self._run(crawl_days, phases)

    def get_status(self) -> dict:
        """获取流水线当前状态。

        Returns:
            {"status": str, "current_phase": str, "state": dict, "errors": list}
        """
        if self._state is None:
            return {
                "status": PipelineStatus.IDLE.value,
                "current_phase": "",
                "state": {},
                "errors": [],
            }
        return {
            "status": self._state["status"],
            "current_phase": self._state["current_phase"],
            "state": dict(self._state),
            "errors": self._state["errors"],
        }

    async def cancel(self):
        """取消正在运行的流水线"""
        if self._task and not self._task.done():
            self._task.cancel()
            if self._state:
                self._state["status"] = PipelineStatus.CANCELLED.value
            logger.info("[pipeline] Cancelled")

    # ── 内部实现 ──────────────────────────────────────────────

    def _build_graph(self):
        """构建 LangGraph StateGraph"""
        graph = StateGraph(dict)

        tools = self.tools
        db = self.db
        scorer = self.scorer
        reporter = self.reporter
        knowledge = self.knowledge

        # 节点必须是 async 函数（不能用 lambda，因为 lambda 不支持 await）
        async def _crawl(state: dict) -> dict:
            return await crawl_node(state, tools, db)

        async def _classify(state: dict) -> dict:
            return await classify_node(state, tools, db)

        async def _score(state: dict) -> dict:
            return await score_node(state, tools, scorer, knowledge, db)

        async def _report(state: dict) -> dict:
            return await report_node(state, tools, reporter, knowledge, db)

        graph.add_node("crawl", _crawl)
        graph.add_node("classify", _classify)
        graph.add_node("score", _score)
        graph.add_node("report", _report)

        graph.set_entry_point("crawl")
        graph.add_edge("crawl", "classify")
        graph.add_edge("classify", "score")
        graph.add_edge("score", "report")
        graph.add_edge("report", END)

        return graph.compile()

    async def _run(
        self,
        crawl_days: int,
        phases: list[str],
    ) -> dict:
        """内部执行逻辑"""
        import uuid

        pipeline_id = str(uuid.uuid4())[:8]

        if self._state and self._state["status"] == PipelineStatus.RUNNING.value:
            return {
                "pipeline_id": pipeline_id,
                "status": "rejected",
                "state": dict(self._state),
                "error": "Pipeline is already running",
            }

        self._state = create_state(crawl_days=crawl_days, phases=phases)
        self._state["status"] = PipelineStatus.RUNNING.value
        self._state["started_at"] = _now_iso()

        try:
            final_state = await self._graph.ainvoke(self._state)
            final_state["status"] = PipelineStatus.COMPLETED.value
            final_state["finished_at"] = _now_iso()
            self._state = final_state
        except asyncio.CancelledError:
            self._state["status"] = PipelineStatus.CANCELLED.value
            self._state["finished_at"] = _now_iso()
        except Exception as e:
            logger.error("[pipeline] Fatal error: %s", e)
            self._state["status"] = PipelineStatus.FAILED.value
            self._state["errors"].append(f"fatal: {e}")
            self._state["finished_at"] = _now_iso()

        logger.info(
            "[pipeline] %s — crawled=%d classified=%d scored=%d reports=%d errors=%d",
            self._state["status"],
            self._state["crawled_count"],
            self._state["classified_count"],
            self._state["scored_count"],
            self._state["report_count"],
            len(self._state["errors"]),
        )

        return {
            "pipeline_id": pipeline_id,
            "status": self._state["status"],
            "state": dict(self._state),
        }


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _now_iso() -> str:
    """返回当前时间 ISO 格式字符串（CST 时区）"""
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")
