"""
Agent 流水线编排 V2 — LangGraph StateGraph

V2 流水线:
  crawl → classify_v2 (6分类) → filter (3 PR类) → score_v2 → draft (4草稿)

与 V1 差异:
  - 使用 ClassifierV2 替代 V1 classify_articles MCP 工具
  - 增加 filter 节点：仅 is_pr_eligible=True 的文章进入后续流程
  - 使用 ScoringAgentV2 替代 V1 scoring（双维度）
  - 使用 DraftGenerator 生成 4 篇 PR 草稿
  - API 端点: POST /api/pipeline/run-v2

使用:
    from agent.pipeline_v2 import PipelineManagerV2

    manager = PipelineManagerV2(tools, classifier_v2, scorer_v2, draft_gen, knowledge, db)
    result = await manager.run_full(crawl_days=1)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from langgraph.graph import END, StateGraph

logger = logging.getLogger("backend.agent.pipeline_v2")


# ═══════════════════════════════════════════════════════════════
# 状态定义
# ═══════════════════════════════════════════════════════════════


class PipelinePhaseV2(StrEnum):
    CRAWL = "crawl"
    CLASSIFY_V2 = "classify_v2"
    SCORE_V2 = "score_v2"
    DRAFT = "draft"


class PipelineStatusV2(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def create_state_v2(crawl_days: int = 1) -> dict:
    """创建 V2 流水线初始状态"""
    return {
        "crawl_days": crawl_days,
        "phases": [p.value for p in PipelinePhaseV2],
        "crawled_count": 0,
        "classified_v2_count": 0,
        "pr_eligible_count": 0,
        "scored_v2_count": 0,
        "draft_count": 0,
        "score_threshold": 80,
        "threshold_adjustment": 0,
        "errors": [],
        "status": PipelineStatusV2.IDLE.value,
        "current_phase": "",
        "started_at": "",
        "finished_at": "",
    }


# ═══════════════════════════════════════════════════════════════
# 流水线节点
# ═══════════════════════════════════════════════════════════════


async def crawl_node_v2(state: dict, tools: dict, db: Any) -> dict:
    """爬取阶段 — 复用 V1 crawl_node"""
    from agent.pipeline import crawl_node

    return await crawl_node(state, tools, db)


async def classify_v2_node(state: dict, classifier: Any, db: Any) -> dict:
    """V2 6分类阶段：调用 ClassifierV2 对 crawled 文章分类"""
    from api.logs import log_pipeline

    state["current_phase"] = PipelinePhaseV2.CLASSIFY_V2.value
    logger.info("[classify_v2] Starting V2 classification")

    try:
        if db is None:
            return state

        query = {
            "pipeline_status": {"$in": ["crawled", "classified"]},
            "$or": [
                {"category_v2": {"$in": ["", None]}},
                {"category_v2": {"$exists": False}},
            ],
        }
        cursor = db["articles"].find(query)
        articles = await cursor.to_list(length=500)

        if not articles:
            logger.info("[classify_v2] No articles to classify")
            return state

        results = await classifier.classify_batch(articles)

        updated = 0
        pr_eligible = 0
        for art, result in zip(articles, results, strict=False):
            try:
                await db["articles"].update_one(
                    {"_id": art["_id"]},
                    {
                        "$set": {
                            "category_v2": result.category,
                            "category_v2_confidence": result.confidence,
                            "category_v2_reason": result.reason,
                            "category_v2_fallback": result.is_fallback,
                            "is_pr_eligible": result.is_pr_eligible,
                        }
                    },
                )
                updated += 1
                if result.is_pr_eligible:
                    pr_eligible += 1
            except Exception as e:
                logger.warning("[classify_v2] DB update failed: %s", e)

        state["classified_v2_count"] = updated
        state["pr_eligible_count"] = pr_eligible
        logger.info(
            "[classify_v2] Done: %d/%d classified, %d PR-eligible",
            updated,
            len(articles),
            pr_eligible,
        )
        log_pipeline(db, "INFO", "classify_v2", f"classified {updated}, {pr_eligible} PR-eligible")

    except Exception as e:
        logger.error("[classify_v2] Phase failed: %s", e)
        state["errors"].append(f"classify_v2: {e}")

    return state


async def score_v2_node(state: dict, scorer: Any, knowledge: Any, db: Any) -> dict:
    """V2 双维度打分阶段：对 PR 候选文章评分"""
    from api.logs import log_pipeline

    state["current_phase"] = PipelinePhaseV2.SCORE_V2.value
    logger.info("[score_v2] Starting V2 scoring")

    try:
        if db is None:
            return state

        await knowledge.load()
        adjust_threshold = getattr(scorer, "adjust_threshold", None)
        if inspect.iscoroutinefunction(adjust_threshold):
            threshold_info = await adjust_threshold(db=db)
            state["score_threshold"] = threshold_info["threshold"]
            state["threshold_adjustment"] = threshold_info["adjustment"]
            logger.info(
                "[score_v2] Threshold adjusted: %d (%+d, %d directional feedbacks)",
                threshold_info["threshold"],
                threshold_info["adjustment"],
                threshold_info["directional_count"],
            )

        cursor = db["articles"].find(
            {
                "is_pr_eligible": True,
                "pr_total_score": None,
            }
        )
        articles = await cursor.to_list(length=500)

        if not articles:
            logger.info("[score_v2] No PR-eligible articles")
            return state

        scored = await scorer.score_batch(articles)
        scored_count = 0
        candidates = 0
        for art, result in zip(articles, scored, strict=False):
            if not result.get("_fallback", True):
                await db["articles"].update_one(
                    {"_id": art["_id"]},
                    {
                        "$set": {
                            "product_relevance": result["product_relevance"],
                            "event_impact": result["event_impact"],
                            "pr_total_score": result["pr_total_score"],
                            "score_reason": result.get("score_reason", ""),
                            "pr_threshold": result.get("pr_threshold", state["score_threshold"]),
                            "threshold_adjustment": result.get(
                                "threshold_adjustment",
                                state["threshold_adjustment"],
                            ),
                        }
                    },
                )
                scored_count += 1
                if result.get("is_pr_candidate"):
                    candidates += 1

        state["scored_v2_count"] = scored_count
        logger.info(
            "[score_v2] Scored: %d/%d, %d PR candidates (>=%d)",
            scored_count,
            len(articles),
            candidates,
            state["score_threshold"],
        )
        log_pipeline(
            db, "INFO", "score_v2", f"scored {scored_count}/{len(articles)}, {candidates} drafted"
        )

    except Exception as e:
        logger.error("[score_v2] Phase failed: %s", e)
        state["errors"].append(f"score_v2: {e}")

    return state


async def draft_node(state: dict, draft_gen: Any, knowledge: Any, db: Any) -> dict:
    """PR 草稿生成阶段：对高分文章生成 4 篇草稿"""

    state["current_phase"] = PipelinePhaseV2.DRAFT.value
    logger.info("[draft] Starting draft generation")

    try:
        if db is None:
            return state

        await knowledge.load()
        score_threshold = int(state.get("score_threshold", 80))
        style_hints = await _load_style_hints(db)

        cursor = db["articles"].find({"pr_total_score": {"$gte": score_threshold}})
        articles = await cursor.to_list(length=30)

        if not articles:
            logger.info("[draft] No articles scored >= %d", score_threshold)
            return state

        draft_count = 0
        for art in articles:
            v2_scores = {
                "product_relevance": art.get("product_relevance", 0),
                "event_impact": art.get("event_impact", 0),
                "pr_total_score": art.get("pr_total_score", 0),
                "score_reason": art.get("score_reason", ""),
            }
            result = await draft_gen.generate(art, v2_scores, style_hints=style_hints)
            if result["ok"] and result["drafts"]:
                await db["articles"].update_one(
                    {"_id": art["_id"]},
                    {
                        "$set": {
                            "pr_drafts": result["drafts"],
                            "pr_template_used": result["drafts"][0]["template"]
                            if result["drafts"]
                            else "",
                        }
                    },
                )
                draft_count += 1

        state["draft_count"] = draft_count
        logger.info("[draft] Generated drafts for %d articles", draft_count)

    except Exception as e:
        logger.error("[draft] Phase failed: %s", e)
        state["errors"].append(f"draft: {e}")

    return state


async def _load_style_hints(db: Any, user_id: str = "local-user") -> str | None:
    """读取用户画像并转换为草稿生成可注入的风格提示。"""
    try:
        profile = await db["user_profiles"].find_one({"user_id": user_id})
        if not profile:
            return None
        from agent.style_profiler import StyleProfiler

        return StyleProfiler(llm=None, db=db).get_style_hints(profile)
    except Exception as exc:
        logger.warning("[draft] Failed to load style hints: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════
# PipelineManagerV2
# ═══════════════════════════════════════════════════════════════


class PipelineManagerV2:
    """V2 流水线生命周期管理器。

    V2 流水线:
      crawl → classify_v2 → score_v2 → draft
    """

    def __init__(
        self,
        tools: dict,
        classifier_v2: Any,  # ClassifierV2
        scorer_v2: Any,  # ScoringAgentV2
        draft_gen: Any,  # DraftGenerator
        knowledge: Any,  # KnowledgeLoader
        db: Any = None,  # AsyncIOMotorDatabase
    ):
        self.tools = tools
        self.classifier_v2 = classifier_v2
        self.scorer_v2 = scorer_v2
        self.draft_gen = draft_gen
        self.knowledge = knowledge
        self.db = db

        self._state: dict | None = None
        self._task: asyncio.Task | None = None
        self._graph = self._build_graph()

    # ── 公开接口 ──────────────────────────────────────────────

    async def run_full(self, crawl_days: int = 1) -> dict:
        """执行全流程 V2 流水线"""
        return await self._run(crawl_days)

    def get_status(self) -> dict:
        if self._state is None:
            return {
                "status": PipelineStatusV2.IDLE.value,
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
        if self._task and not self._task.done():
            self._task.cancel()
            if self._state:
                self._state["status"] = PipelineStatusV2.CANCELLED.value

    # ── 内部实现 ──────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(dict)
        ctools = self.tools
        cdb = self.db
        cclassifier = self.classifier_v2
        cscorer = self.scorer_v2
        cdraft_gen = self.draft_gen
        cknowledge = self.knowledge

        async def _crawl(state: dict) -> dict:
            return await crawl_node_v2(state, ctools, cdb)

        async def _classify_v2(state: dict) -> dict:
            return await classify_v2_node(state, cclassifier, cdb)

        async def _score_v2(state: dict) -> dict:
            return await score_v2_node(state, cscorer, cknowledge, cdb)

        async def _draft(state: dict) -> dict:
            return await draft_node(state, cdraft_gen, cknowledge, cdb)

        graph.add_node("crawl", _crawl)
        graph.add_node("classify_v2", _classify_v2)
        graph.add_node("score_v2", _score_v2)
        graph.add_node("draft", _draft)

        graph.set_entry_point("crawl")
        graph.add_edge("crawl", "classify_v2")
        graph.add_edge("classify_v2", "score_v2")
        graph.add_edge("score_v2", "draft")
        graph.add_edge("draft", END)

        return graph.compile()

    async def _run(self, crawl_days: int) -> dict:
        import uuid

        pipeline_id = str(uuid.uuid4())[:8]

        if self._state and self._state["status"] == PipelineStatusV2.RUNNING.value:
            return {
                "pipeline_id": pipeline_id,
                "status": "rejected",
                "error": "Pipeline is already running",
            }

        self._state = create_state_v2(crawl_days=crawl_days)
        self._state["status"] = PipelineStatusV2.RUNNING.value
        self._state["started_at"] = _now_iso()

        try:
            final_state = await self._graph.ainvoke(self._state)
            final_state["status"] = PipelineStatusV2.COMPLETED.value
            final_state["finished_at"] = _now_iso()
            self._state = final_state
        except asyncio.CancelledError:
            self._state["status"] = PipelineStatusV2.CANCELLED.value
            self._state["finished_at"] = _now_iso()
        except Exception as e:
            logger.error("[pipeline_v2] Fatal: %s", e)
            self._state["status"] = PipelineStatusV2.FAILED.value
            self._state["errors"].append(f"fatal: {e}")
            self._state["finished_at"] = _now_iso()

        logger.info(
            "[pipeline_v2] %s — crawled=%d classified=%d pr_eligible=%d scored=%d drafts=%d",
            self._state["status"],
            self._state["crawled_count"],
            self._state["classified_v2_count"],
            self._state.get("pr_eligible_count", 0),
            self._state["scored_v2_count"],
            self._state["draft_count"],
        )

        return {
            "pipeline_id": pipeline_id,
            "status": self._state["status"],
            "state": dict(self._state),
        }


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")
