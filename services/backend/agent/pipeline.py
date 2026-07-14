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
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from langgraph.graph import END, StateGraph

logger = logging.getLogger("backend.agent.pipeline")


# ═══════════════════════════════════════════════════════════════
# 状态定义
# ═══════════════════════════════════════════════════════════════


class PipelinePhase(StrEnum):
    CRAWL = "crawl"
    CLASSIFY = "classify"
    SCORE = "score"
    REPORT = "report"


class PipelineStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def create_state(
    crawl_days: int = 1,
    phases: list[str] | None = None,
    user_id: str = "",
    trace_id: str = "",
    username: str = "",
) -> dict:
    """创建流水线初始状态（普通 dict，兼容 LangGraph）"""
    return {
        "crawl_days": crawl_days,
        "user_id": user_id,
        "trace_id": trace_id,
        "username": username or user_id,
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


# 后台全文抓取任务引用集合（防止 asyncio.create_task 被 GC 回收）
_fulltext_tasks: set = set()


async def _fetch_fulltext_background(
    db: Any, articles: list[dict], trace_id: str
) -> None:
    """后台异步抓取海外新闻全文（不阻塞流水线）

    调用 mcp-crawl 的批量抓取端点，含反风控策略：
    - 域名并发限制（每域名 2 并发）
    - 随机延迟 1-3 秒
    - 失败重试 3 次（指数退避）
    """
    import httpx

    urls = [a["url"] for a in articles]
    logger.info(
        "[fulltext-bg] Start: %d articles, trace=%s", len(urls), trace_id
    )
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                "http://mcp-crawl:8101/fetch-fulltext-batch",
                json=urls,
            )
        if resp.status_code != 200:
            logger.warning(
                "[fulltext-bg] mcp-crawl returned %s, trace=%s",
                resp.status_code,
                trace_id,
            )
            return

        data = resp.json().get("data", {})
        updated = 0
        for art in articles:
            content = data.get(art["url"], "")
            if content:
                await db["articles"].update_one(
                    {"url_hash": art["url_hash"]},
                    {"$set": {"content_md": content[:50000]}},
                )
                updated += 1

        logger.info(
            "[fulltext-bg] Done: %d/%d updated, trace=%s",
            updated,
            len(articles),
            trace_id,
        )
    except Exception as e:
        logger.warning("[fulltext-bg] failed: %s, trace=%s", e, trace_id)


async def _log_stage(
    state: dict,
    db: Any,
    phase: str,
    message: str,
    *,
    level: str = "INFO",
    action: str = "complete",
    duration_ms: int | None = None,
    detail: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    """使用状态中的租户和 trace 上下文写入阶段日志。"""

    from api.logs import log_pipeline

    await log_pipeline(
        db,
        level,
        phase,
        message,
        user_id=state["user_id"],
        username=state.get("username"),
        trace_id=state.get("trace_id"),
        action=action,
        duration_ms=duration_ms,
        detail=detail,
        error=error,
    )


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
    import uuid

    batch_id = uuid.uuid4().hex[:12]
    state["batch_id"] = batch_id
    started = time.perf_counter()
    state["current_phase"] = PipelinePhase.CRAWL.value
    logger.info("[crawl] Starting crawl phase (days=%d, batch=%s)", state["crawl_days"], batch_id)
    await _log_stage(
        state,
        db,
        "crawl",
        f"start crawl (days={state['crawl_days']})",
        action="start",
        detail={"days": state["crawl_days"], "batch_id": batch_id},
    )

    if PipelinePhase.CRAWL.value not in state["phases"]:
        logger.info("[crawl] Skipped (not in selected phases)")
        await _log_stage(state, db, "crawl", "crawl skipped", action="skip")
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

        # 2. 获取微信公众号文章（从 Atom feed，含 author 信息）
        try:
            import httpx

            atom_url = "http://49.232.145.182:4001/feeds/all.atom"
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(atom_url)
                import xml.etree.ElementTree as ET

                NS = {"atom": "http://www.w3.org/2005/Atom"}
                root = ET.fromstring(resp.text)
                entries = root.findall("atom:entry", NS)
                for entry in entries:
                    title_el = entry.find("atom:title", NS)
                    link_el = entry.find("atom:link", NS)
                    author_el = entry.find("atom:author/atom:name", NS)
                    updated_el = entry.find("atom:updated", NS)
                    title = title_el.text if title_el is not None else ""
                    url = link_el.get("href", "") if link_el is not None else ""
                    source_name = author_el.text if author_el is not None else "微信公众号"
                    pub_date = (
                        updated_el.text[:10].replace("-", "年", 1).replace("-", "月") + "日"
                        if updated_el is not None and updated_el.text
                        else ""
                    )
                    if url:
                        articles.append(
                            {
                                "title": title,
                                "url": url,
                                "source": source_name,
                                "source_type": "wechat_mp",
                                "published_at": pub_date,
                                "summary": "",
                            }
                        )
                logger.info("[crawl] WeWe Atom: %d articles", len(entries))
        except Exception as e:
            logger.warning("[crawl] WeWe fetch failed (non-critical): %s", e)

        # 3. 去重 + 入库
        if db is not None and articles:
            saved_count = 0
            skipped = 0
            for art in articles:
                try:
                    url = art.get("url", "")
                    if not url:
                        continue
                    import hashlib

                    url_hash = art.get("url_hash") or hashlib.md5(url.encode()).hexdigest()

                    # 去重：检查是否已存在
                    existing = await db["articles"].find_one({"url_hash": url_hash})
                    if existing:
                        skipped += 1
                        continue

                    # 日期格式化
                    pub = art.get("published_at", "")
                    if pub and "-" in pub:
                        parts = pub.split("-")
                        if len(parts) == 3:
                            pub = f"{parts[0]}年{int(parts[1])}月{int(parts[2])}日"

                    doc = {
                        "url_hash": url_hash,
                        "title": art.get("title", ""),
                        "url": art.get("url", ""),
                        "source": art.get("source", ""),
                        "source_type": art.get("source_type", "overseas_news"),
                        "published_at": pub if pub else art.get("published_at", ""),
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
                        "batch_id": state.get("batch_id", ""),
                    }
                    # upsert: 存在则更新，不存在则插入
                    await db["articles"].update_one(
                        {"url_hash": url_hash},
                        {"$setOnInsert": doc},
                        upsert=True,
                    )
                    saved_count += 1
                except Exception as e:
                    logger.warning("[crawl] Failed to save: %s", e)

            state["crawled_count"] = saved_count
            logger.info(
                "[crawl] Saved %d new, skipped %d duplicates (of %d total)",
                saved_count,
                skipped,
                len(articles),
            )

            # 收集新入库的海外文章 URL，异步抓取全文
            overseas_urls: list[dict] = []
            for art in articles:
                if art.get("source_type", "overseas_news") == "overseas_news":
                    url = art.get("url", "")
                    url_hash = art.get("url_hash") or hashlib.md5(url.encode()).hexdigest()
                    if url:
                        overseas_urls.append({"url_hash": url_hash, "url": url})

            if overseas_urls:
                task = asyncio.create_task(
                    _fetch_fulltext_background(db, overseas_urls, state.get("trace_id", ""))
                )
                _fulltext_tasks.add(task)
                task.add_done_callback(_fulltext_tasks.discard)
        else:
            state["crawled_count"] = len(articles)
            logger.info("[crawl] %d articles crawled (no DB, not saved)", len(articles))

        duration_ms = int((time.perf_counter() - started) * 1000)
        await _log_stage(
            state,
            db,
            "crawl",
            f"crawl completed: {state['crawled_count']} new articles",
            duration_ms=duration_ms,
            detail={
                "source": "overseas,wewe",
                "new_count": state["crawled_count"],
                "total_count": len(articles),
                "duration_ms": duration_ms,
            },
        )

    except Exception as e:
        logger.error("[crawl] Phase failed: %s", e)
        state["errors"].append(f"crawl: {e}")
        from api.logs import build_log_error

        duration_ms = int((time.perf_counter() - started) * 1000)
        await _log_stage(
            state,
            db,
            "crawl",
            "crawl failed",
            level="ERROR",
            action="error",
            duration_ms=duration_ms,
            error=build_log_error(e),
        )

    return state


async def classify_node(state: dict, tools: dict, db: Any) -> dict:
    """分类阶段：对已爬取文章进行 AI 分类。

    从 MongoDB 读取 pipeline_status="crawled" 的文章，调用 mcp-crawl 分类。
    """
    started = time.perf_counter()
    state["current_phase"] = PipelinePhase.CLASSIFY.value
    logger.info("[classify] Starting classify phase")
    await _log_stage(state, db, "classify", "classify started", action="start")

    if PipelinePhase.CLASSIFY.value not in state["phases"]:
        logger.info("[classify] Skipped (not in selected phases)")
        await _log_stage(state, db, "classify", "classify skipped", action="skip")
        return state

    try:
        if db is None:
            logger.info("[classify] No DB, skipping")
            await _log_stage(state, db, "classify", "classify skipped: no database", action="skip")
            return state

        cursor = db["articles"].find({"pipeline_status": "crawled"})
        raw_articles = await cursor.to_list(length=100)

        if not raw_articles:
            logger.info("[classify] No articles to classify")
            await _log_stage(state, db, "classify", "classify skipped: no articles", action="skip")
            return state

        # 并行分类：每篇文章独立调用 classify 工具
        import json as _json

        sem = asyncio.Semaphore(10)

        async def _classify_one(art):
            async with sem:
                try:
                    ajson = _json.dumps(
                        [
                            {
                                "title": art.get("title", ""),
                                "url": art.get("url", ""),
                                "source": art.get("source", ""),
                                "summary": art.get("summary", ""),
                            }
                        ],
                        ensure_ascii=False,
                    )
                    r = await tools["classify_articles"].ainvoke(
                        {"payload": {"articles_json": ajson, "batch_size": 1}}
                    )
                    if r.get("ok") and r.get("data"):
                        data = r["data"]
                        classified = data.get("classified", []) if isinstance(data, dict) else data
                        if classified:
                            cls = classified[0]
                            await db["articles"].update_one(
                                {"_id": art["_id"]},
                                {
                                    "$set": {
                                        "is_ai_security": cls.get("is_ai_security", False),
                                        "is_agent_security": cls.get("is_agent_security", False),
                                        "category": cls.get("category", ""),
                                        "summary_cn": cls.get("summary_cn", ""),
                                        "pipeline_status": "classified",
                                    }
                                },
                            )
                            return True
                except Exception as e:
                    logger.warning("[classify] Article failed: %s", e)
                return False

        results = await asyncio.gather(*[_classify_one(a) for a in raw_articles])
        classified_count = sum(1 for r in results if r)
        state["classified_count"] = classified_count
        logger.info(
            "[classify] Parallel classified %d/%d articles", classified_count, len(raw_articles)
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        await _log_stage(
            state,
            db,
            "classify",
            f"parallel classified {classified_count}/{len(raw_articles)}",
            duration_ms=duration_ms,
            detail={"article_count": len(raw_articles), "classified_count": classified_count},
        )

    except Exception as e:
        logger.error("[classify] Phase failed: %s", e)
        state["errors"].append(f"classify: {e}")
        from api.logs import build_log_error

        await _log_stage(
            state,
            db,
            "classify",
            "classify failed",
            level="ERROR",
            action="error",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=build_log_error(e),
        )

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
    started = time.perf_counter()
    state["current_phase"] = PipelinePhase.SCORE.value
    logger.info("[score] Starting score phase")
    await _log_stage(state, db, "score", "score started", action="start")

    if PipelinePhase.SCORE.value not in state["phases"]:
        logger.info("[score] Skipped (not in selected phases)")
        await _log_stage(state, db, "score", "score skipped", action="skip")
        return state

    try:
        if db is None:
            logger.info("[score] No DB, skipping")
            await _log_stage(state, db, "score", "score skipped: no database", action="skip")
            return state

        # 确保知识库已加载
        await knowledge.load()

        cursor = db["articles"].find({"pipeline_status": "classified", "is_ai_security": True})
        raw_articles = await cursor.to_list(length=50)

        if not raw_articles:
            logger.info("[score] No articles to score")
            await _log_stage(state, db, "score", "score skipped: no articles", action="skip")
            return state

        # LLM 批量打分
        scored = await scorer.score_batch(raw_articles)
        scored_count = 0
        for art, scores in zip(raw_articles, scored, strict=False):
            if not scores.get("_fallback", True):
                await db["articles"].update_one(
                    {"_id": art["_id"]},
                    {
                        "$set": {
                            "ai_relevance_score": scores["ai_relevance_score"],
                            "reportability_score": scores["reportability_score"],
                            "score_reason": scores.get("score_reason", ""),
                            "pipeline_status": "scored",
                        }
                    },
                )
                scored_count += 1
        state["scored_count"] = scored_count
        logger.info("[score] LLM scored %d/%d articles", scored_count, len(raw_articles))
        duration_ms = int((time.perf_counter() - started) * 1000)
        await _log_stage(
            state,
            db,
            "score",
            f"scored {scored_count}/{len(raw_articles)} articles",
            duration_ms=duration_ms,
            detail={"article_count": len(raw_articles), "scored_count": scored_count},
        )

    except Exception as e:
        logger.error("[score] Phase failed: %s", e)
        state["errors"].append(f"score: {e}")
        from api.logs import build_log_error

        await _log_stage(
            state,
            db,
            "score",
            "score failed",
            level="ERROR",
            action="error",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=build_log_error(e),
        )

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
    started = time.perf_counter()
    state["current_phase"] = PipelinePhase.REPORT.value
    logger.info("[report] Starting report generation phase")
    await _log_stage(state, db, "report", "report generation started", action="start")

    if PipelinePhase.REPORT.value not in state["phases"]:
        logger.info("[report] Skipped (not in selected phases)")
        await _log_stage(state, db, "report", "report skipped", action="skip")
        return state

    try:
        if db is None:
            logger.info("[report] No DB, skipping")
            await _log_stage(state, db, "report", "report skipped: no database", action="skip")
            return state

        await knowledge.load()

        from agent.style_profiler import load_style_hints

        style_hints = await load_style_hints(db, state["user_id"])
        logger.info(
            "[report] style_hints injected=%s user_id=%s",
            bool(style_hints),
            state["user_id"],
        )

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
            await _log_stage(state, db, "report", "report skipped: no candidates", action="skip")
            return state

        report_count = 0
        for art in high_value:
            scores = {
                "ai_relevance_score": art.get("ai_relevance_score", 0),
                "reportability_score": art.get("reportability_score", 0),
                "total_score": art.get("_total_score", 0),
                "score_reason": art.get("score_reason", ""),
            }
            result = await reporter.generate_report(
                art,
                scores,
                style_hints=style_hints,
            )
            if result["ok"]:
                report_count += 1

        state["report_count"] = report_count
        logger.info("[report] Generated %d reports", report_count)
        duration_ms = int((time.perf_counter() - started) * 1000)
        await _log_stage(
            state,
            db,
            "report",
            f"generated {report_count} reports",
            duration_ms=duration_ms,
            detail={
                "candidate_count": len(high_value),
                "report_count": report_count,
                "style_hints_used": bool(style_hints),
            },
        )

    except Exception as e:
        logger.error("[report] Phase failed: %s", e)
        state["errors"].append(f"report: {e}")
        from api.logs import build_log_error

        await _log_stage(
            state,
            db,
            "report",
            "report generation failed",
            level="ERROR",
            action="error",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=build_log_error(e),
        )

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
        scorer: Any,  # ScoringAgent
        reporter: Any,  # ReportAgent
        knowledge: Any,  # KnowledgeLoader
        db: Any = None,  # AsyncIOMotorDatabase
    ):
        self.tools = tools
        self.scorer = scorer
        self.reporter = reporter
        self.knowledge = knowledge
        self.db = db

        # 状态
        self._state: dict | None = None
        self._task: asyncio.Task | None = None
        self._graph = self._build_graph()

    # ── 公开接口 ──────────────────────────────────────────────

    async def run_full(
        self,
        crawl_days: int = 1,
        user_id: str = "",
        trace_id: str = "",
        username: str = "",
    ) -> dict:
        """执行全流程流水线。

        Args:
            crawl_days: 爬取天数

        Returns:
            {"pipeline_id": str, "status": str, "state": dict}
        """
        phases = [p.value for p in PipelinePhase]
        return await self._run(crawl_days, phases, user_id, trace_id, username)

    async def run_phase(
        self,
        phase: str,
        crawl_days: int = 1,
        user_id: str = "",
        trace_id: str = "",
        username: str = "",
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
        return await self._run(crawl_days, phases, user_id, trace_id, username)

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
        user_id: str,
        trace_id: str,
        username: str,
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

        self._state = create_state(
            crawl_days=crawl_days,
            phases=phases,
            user_id=user_id,
            trace_id=trace_id,
            username=username,
        )
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
