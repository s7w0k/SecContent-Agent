"""
Agent 流水线编排 V2 — LangGraph StateGraph

V2 流水线:
  crawl → [enrich] → classify_v2 → filter → score_v2 → draft
  → quality_check → [rewrite] → review → END

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
import time
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from agent.checkpointer import create_checkpointer, supports_mongodb_checkpoints
from agent.draft_reviewer import compute_content_hash
from agent.pipeline_state import PipelineStateManager
from clients.mcp_crawl import McpCrawlClient, RequestContext
from langgraph.graph import END, StateGraph
from models.draft_review import DraftReview
from models.pr_template import EffectivePRTemplate

logger = logging.getLogger("backend.agent.pipeline_v2")


# ═══════════════════════════════════════════════════════════════
# 状态定义
# ═══════════════════════════════════════════════════════════════


class PipelinePhaseV2(StrEnum):
    CRAWL = "crawl"
    ENRICH = "enrich"
    CLASSIFY_V2 = "classify_v2"
    FILTER = "filter"
    SCORE_V2 = "score_v2"
    DRAFT = "draft"
    QUALITY_CHECK = "quality_check"
    REWRITE = "rewrite"
    REVIEW = "review"


class PipelineStatusV2(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def create_state_v2(
    crawl_days: int = 1,
    user_id: str = "",
    trace_id: str = "",
    username: str = "",
    request_id: str = "",
) -> dict:
    """创建 V2 流水线初始状态"""
    return {
        "crawl_days": crawl_days,
        "user_id": user_id,
        "trace_id": trace_id,
        "username": username or user_id,
        "request_id": request_id,
        "phases": [p.value for p in PipelinePhaseV2],
        "crawled_count": 0,
        "enriched_count": 0,
        "classified_v2_count": 0,
        "low_confidence_count": 0,
        "pr_eligible_count": 0,
        "scored_v2_count": 0,
        "draft_count": 0,
        "rewritten_count": 0,
        "review_count": 0,
        "review_failed_count": 0,
        "review_reused_count": 0,
        "score_threshold": 80,
        "threshold_adjustment": 0,
        "needs_enrich": False,
        "enriched": False,
        "low_confidence_articles": [],
        "score_anomaly": False,
        "score_retried": False,
        "needs_rewrite": [],
        "frozen_templates": {},
        "errors": [],
        "status": PipelineStatusV2.IDLE.value,
        "current_phase": "",
        "started_at": "",
        "finished_at": "",
    }


# ═══════════════════════════════════════════════════════════════
# 流水线节点
# ═══════════════════════════════════════════════════════════════


async def crawl_node_v2(
    state: dict,
    tools: dict,
    db: Any,
    crawl_client: McpCrawlClient | None = None,
) -> dict:
    """爬取阶段 — 复用 V1 crawl_node"""
    from agent.pipeline import crawl_node

    state = await crawl_node(state, tools, db, crawl_client)
    if db is not None:
        incomplete = await db["articles"].count_documents(_incomplete_article_query())
        state["needs_enrich"] = incomplete > 0 and not state.get("enriched", False)
        state["incomplete_article_count"] = incomplete
    return state


def _incomplete_article_query() -> dict[str, Any]:
    """MongoDB query for crawled articles whose body is shorter than 200 chars."""
    return {
        "pipeline_status": "crawled",
        "url": {"$nin": [None, ""]},
        "$expr": {
            "$lt": [
                {"$strLenCP": {"$ifNull": ["$content_md", ""]}},
                200,
            ]
        },
    }


async def _fetch_fulltext_batch(
    articles: list[dict],
    crawl_client: McpCrawlClient | None = None,
    context: RequestContext | None = None,
) -> dict[str, str]:
    """Fetch full text from mcp-crawl and return a URL-to-content mapping."""
    urls = [article["url"] for article in articles if article.get("url")]
    if not urls or crawl_client is None:
        return {}
    return await crawl_client.fetch_fulltext_batch(urls, context)


async def enrich_node(
    state: dict,
    tools: dict,
    db: Any,
    crawl_client: McpCrawlClient | None = None,
) -> dict:
    """补爬正文不足 200 字的文章，并在一次执行中最多触发一次。"""
    del tools  # Reserved for a future MCP tool abstraction.
    state["current_phase"] = PipelinePhaseV2.ENRICH.value
    state["enriched"] = True
    state["needs_enrich"] = False
    if db is None:
        return state

    cursor = db["articles"].find(_incomplete_article_query())
    articles = await cursor.to_list(length=100)
    if not articles:
        return state

    try:
        context = RequestContext.create(
            request_id=state.get("request_id"),
            trace_id=state.get("trace_id"),
            initiator_user_id=state.get("user_id"),
        )
        content_by_url = await _fetch_fulltext_batch(articles, crawl_client, context)
        enriched_count = 0
        for article in articles:
            content = content_by_url.get(article.get("url", ""), "")
            if len(content) < 200:
                continue
            await db["articles"].update_one(
                {"_id": article["_id"]},
                {"$set": {"content_md": content[:50000]}},
            )
            enriched_count += 1
        state["enriched_count"] = enriched_count
        logger.info("[enrich] Enriched %d/%d articles", enriched_count, len(articles))
    except Exception as exc:
        logger.warning("[enrich] Full-text enrichment failed: %s", exc)
        state["errors"].append(f"enrich: {exc}")
    return state


async def classify_v2_node(state: dict, classifier: Any, db: Any) -> dict:
    """V2 6分类阶段：调用 ClassifierV2 对 crawled 文章分类"""
    from agent.pipeline import _log_stage

    started = time.perf_counter()
    state["current_phase"] = PipelinePhaseV2.CLASSIFY_V2.value
    logger.info("[classify_v2] Starting V2 classification")
    await _log_stage(state, db, "classify_v2", "V2 classification started", action="start")

    try:
        if db is None:
            await _log_stage(
                state, db, "classify_v2", "V2 classification skipped: no database", action="skip"
            )
            return state

        query = {
            "pipeline_status": {"$in": ["pending", "crawled", "classified"]},
            "$or": [
                {"category_v2": {"$in": ["", None]}},
                {"category_v2": {"$exists": False}},
                {"is_ai_agent_security_relevant": {"$exists": False}},
            ],
        }
        cursor = db["articles"].find(query)
        articles = await cursor.to_list(length=500)

        if not articles:
            logger.info("[classify_v2] No articles to classify")
            await _log_stage(
                state, db, "classify_v2", "V2 classification skipped: no articles", action="skip"
            )
            return state

        results = await classifier.classify_batch(
            articles,
            user_id=state.get("user_id", ""),
            trace_id=state.get("trace_id", ""),
            task_id=state.get("task_id", ""),
        )

        updated = 0
        low_confidence_articles: list[str] = []
        for art, result in zip(articles, results, strict=False):
            try:
                is_low_confidence = result.confidence < 60
                await db["articles"].update_one(
                    {"_id": art["_id"]},
                    {
                        "$set": {
                            "category_v2": result.category,
                            "category_v2_confidence": result.confidence,
                            "category_v2_reason": result.reason,
                            "category_v2_fallback": result.is_fallback,
                            "category_v2_low_confidence": is_low_confidence,
                            "is_pr_eligible": result.is_pr_eligible,
                            "is_ai_agent_security_relevant": getattr(
                                result,
                                "is_relevant",
                                result.category != "不相关",
                            ),
                            "ai_agent_security_relevance_confidence": getattr(
                                result,
                                "relevance_confidence",
                                result.confidence,
                            ),
                            "ai_agent_security_relevance_reason": getattr(
                                result,
                                "relevance_reason",
                                result.reason,
                            ),
                        }
                    },
                )
                updated += 1
                if is_low_confidence:
                    low_confidence_articles.append(str(art.get("url_hash") or art["_id"]))
            except Exception as e:
                logger.warning("[classify_v2] DB update failed: %s", e)

        state["classified_v2_count"] = updated
        state["low_confidence_articles"] = low_confidence_articles
        state["low_confidence_count"] = len(low_confidence_articles)
        logger.info(
            "[classify_v2] Done: %d/%d classified, %d low-confidence",
            updated,
            len(articles),
            len(low_confidence_articles),
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        await _log_stage(
            state,
            db,
            "classify_v2",
            f"classified {updated}, {len(low_confidence_articles)} low-confidence",
            duration_ms=duration_ms,
            detail={
                "article_count": len(articles),
                "classified_count": updated,
                "low_confidence_count": len(low_confidence_articles),
            },
        )

    except Exception as e:
        logger.error("[classify_v2] Phase failed: %s", e)
        state["errors"].append(f"classify_v2: {e}")
        from api.logs import build_log_error

        await _log_stage(
            state,
            db,
            "classify_v2",
            "V2 classification failed",
            level="ERROR",
            action="error",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=build_log_error(e),
        )

    return state


async def filter_node(state: dict, db: Any) -> dict:
    """独立统计可进入评分和草稿阶段的 PR 候选文章。"""
    state["current_phase"] = PipelinePhaseV2.FILTER.value
    if db is None:
        return state
    count = await db["articles"].count_documents({"is_pr_eligible": True})
    state["pr_eligible_count"] = count
    logger.info("[filter] %d PR-eligible articles", count)
    return state


async def score_v2_node(state: dict, scorer: Any, knowledge: Any, db: Any) -> dict:
    """V2 双维度打分阶段：对 PR 候选文章评分"""
    from agent.pipeline import _log_stage

    started = time.perf_counter()
    state["current_phase"] = PipelinePhaseV2.SCORE_V2.value
    is_retry = bool(state.get("score_anomaly") and not state.get("score_retried"))
    if is_retry:
        state["score_retried"] = True
    state["score_anomaly"] = False
    logger.info("[score_v2] Starting V2 scoring")
    await _log_stage(state, db, "score_v2", "V2 scoring started", action="start")

    try:
        if db is None:
            await _log_stage(
                state, db, "score_v2", "V2 scoring skipped: no database", action="skip"
            )
            return state

        await knowledge.load()
        adjust_threshold = getattr(scorer, "adjust_threshold", None)
        if inspect.iscoroutinefunction(adjust_threshold):
            threshold_info = await adjust_threshold(db=db, user_id=state["user_id"])
            state["score_threshold"] = threshold_info["threshold"]
            state["threshold_adjustment"] = threshold_info["adjustment"]
            logger.info(
                "[score_v2] Threshold adjusted: %d (%+d, %d directional feedbacks)",
                threshold_info["threshold"],
                threshold_info["adjustment"],
                threshold_info["directional_count"],
            )

        score_query: dict[str, Any] = {"is_pr_eligible": True}
        if is_retry:
            score_query["$or"] = [
                {"pr_total_score": 0},
                {"pr_total_score": {"$gt": 190}},
            ]
        else:
            score_query["pr_total_score"] = None
        cursor = db["articles"].find(score_query)
        articles = await cursor.to_list(length=500)

        if not articles:
            logger.info("[score_v2] No PR-eligible articles")
            await _log_stage(
                state, db, "score_v2", "V2 scoring skipped: no articles", action="skip"
            )
            return state

        scored = await scorer.score_batch(
            articles,
            threshold=state["score_threshold"],
            threshold_adjustment=state["threshold_adjustment"],
            user_id=state.get("user_id", ""),
            trace_id=state.get("trace_id", ""),
            task_id=state.get("task_id", ""),
        )
        scored_count = 0
        candidates = 0
        anomaly_count = 0
        for art, result in zip(articles, scored, strict=False):
            if not result.get("_fallback", True):
                total_score = result["pr_total_score"]
                await db["articles"].update_one(
                    {"_id": art["_id"]},
                    {
                        "$set": {
                            "product_relevance": result["product_relevance"],
                            "event_impact": result["event_impact"],
                            "pr_total_score": result["pr_total_score"],
                            "score_reason": result.get("score_reason", ""),
                            "product_scores": result.get("product_scores", []),
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
                if total_score == 0 or total_score > 190:
                    anomaly_count += 1

        state["scored_v2_count"] = scored_count
        state["score_anomaly"] = anomaly_count > 0
        logger.info(
            "[score_v2] Scored: %d/%d, %d candidates, %d anomalies (>=%d)",
            scored_count,
            len(articles),
            candidates,
            anomaly_count,
            state["score_threshold"],
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        await _log_stage(
            state,
            db,
            "score_v2",
            f"scored {scored_count}/{len(articles)}, {candidates} drafted",
            duration_ms=duration_ms,
            detail={
                "article_count": len(articles),
                "scored_count": scored_count,
                "candidate_count": candidates,
                "anomaly_count": anomaly_count,
                "is_retry": is_retry,
                "threshold": state["score_threshold"],
            },
        )

    except Exception as e:
        logger.error("[score_v2] Phase failed: %s", e)
        state["errors"].append(f"score_v2: {e}")
        from api.logs import build_log_error

        await _log_stage(
            state,
            db,
            "score_v2",
            "V2 scoring failed",
            level="ERROR",
            action="error",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=build_log_error(e),
        )

    return state


async def draft_node(
    state: dict,
    draft_gen: Any,
    knowledge: Any,
    db: Any,
    template_repository: Any = None,
) -> dict:
    """PR 草稿生成阶段：对高分文章生成 4 篇草稿"""

    from agent.pipeline import _log_stage

    started = time.perf_counter()
    state["current_phase"] = PipelinePhaseV2.DRAFT.value
    logger.info("[draft] Starting draft generation")
    await _log_stage(state, db, "draft", "draft generation started", action="start")

    try:
        if db is None:
            await _log_stage(state, db, "draft", "draft skipped: no database", action="skip")
            return state

        await knowledge.load()
        score_threshold = int(state.get("score_threshold", 80))
        style_hints = await _load_style_hints(db, state["user_id"])
        system_prompt_template = await _load_custom_system_prompt(db, state["user_id"])

        cursor = db["articles"].find({"pr_total_score": {"$gte": score_threshold}})
        articles = await cursor.to_list(length=30)

        if not articles:
            logger.info("[draft] No articles scored >= %d", score_threshold)
            await _log_stage(state, db, "draft", "draft skipped: no candidates", action="skip")
            return state

        draft_count = 0
        logged_categories: set[str] = set()
        for art in articles:
            v2_scores = {
                "product_relevance": art.get("product_relevance", 0),
                "event_impact": art.get("event_impact", 0),
                "pr_total_score": art.get("pr_total_score", 0),
                "score_reason": art.get("score_reason", ""),
            }
            category_v2 = art.get("category_v2", "")
            templates = await _templates_for_category(
                state,
                template_repository,
                category_v2,
            )
            if templates is not None and category_v2 not in logged_categories:
                await _log_stage(
                    state,
                    db,
                    "draft",
                    "frozen PR templates selected",
                    action="template_resolve",
                    detail={
                        "category_v2": category_v2,
                        "templates": _template_log_metadata(templates),
                    },
                )
                logged_categories.add(category_v2)
            result = await draft_gen.generate(
                art,
                v2_scores,
                style_hints=style_hints,
                templates=templates,
                system_prompt_template=system_prompt_template,
            )
            if result["ok"] and result["drafts"]:
                now = datetime.now(UTC)
                await db["user_drafts"].update_one(
                    {
                        "user_id": state["user_id"],
                        "article_url_hash": art["url_hash"],
                    },
                    {
                        "$set": {
                            "drafts": result["drafts"],
                            "updated_at": now,
                        },
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                )
                draft_count += 1

        state["draft_count"] = draft_count
        logger.info("[draft] Generated drafts for %d articles", draft_count)
        duration_ms = int((time.perf_counter() - started) * 1000)
        await _log_stage(
            state,
            db,
            "draft",
            f"generated drafts for {draft_count} articles",
            duration_ms=duration_ms,
            detail={
                "article_count": len(articles),
                "draft_count": draft_count,
                "style_hints_used": bool(style_hints),
                "duration_ms": duration_ms,
            },
        )

    except Exception as e:
        logger.error("[draft] Phase failed: %s", e)
        state["errors"].append(f"draft: {e}")
        from api.logs import build_log_error

        await _log_stage(
            state,
            db,
            "draft",
            "draft generation failed",
            level="ERROR",
            action="error",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=build_log_error(e),
        )

    return state


async def quality_check_node(state: dict, db: Any) -> dict:
    """使用确定性启发式规则标记缺字段或正文不足 300 字的草稿。"""
    state["current_phase"] = PipelinePhaseV2.QUALITY_CHECK.value
    state["needs_rewrite"] = []
    if db is None:
        return state

    cursor = db["user_drafts"].find({"user_id": state["user_id"]})
    draft_documents = await cursor.to_list(length=30)
    needs_rewrite: list[dict[str, Any]] = []
    for document in draft_documents:
        for position, draft in enumerate(document.get("drafts", [])):
            content = (draft.get("content_md") or "").strip()
            if not draft.get("title") or not content:
                reason = "missing_fields"
            elif len(content) < 300:
                reason = "too_short"
            else:
                continue
            needs_rewrite.append(
                {
                    "url_hash": document["article_url_hash"],
                    "index": draft.get("index", position + 1),
                    "position": position,
                    "reason": reason,
                }
            )

    state["needs_rewrite"] = needs_rewrite
    logger.info("[quality_check] %d drafts need rewrite", len(needs_rewrite))
    return state


async def rewrite_node(
    state: dict,
    draft_gen: Any,
    knowledge: Any,
    db: Any,
    template_repository: Any = None,
) -> dict:
    """重新生成质量不达标的草稿，并替换原数组中的对应版本。"""
    state["current_phase"] = PipelinePhaseV2.REWRITE.value
    state["rewritten_count"] = 0
    if db is None:
        return state

    await knowledge.load()
    base_style_hints = await _load_style_hints(db, state["user_id"])
    system_prompt_template = await _load_custom_system_prompt(db, state["user_id"])
    rewritten_count = 0
    for item in state.get("needs_rewrite", []):
        article = await db["articles"].find_one({"url_hash": item["url_hash"]})
        if not article:
            continue
        scores = {
            "product_relevance": article.get("product_relevance", 0),
            "event_impact": article.get("event_impact", 0),
            "pr_total_score": article.get("pr_total_score", 0),
            "score_reason": article.get("score_reason", ""),
        }
        reflection = (
            f"反思重写要求：上一版草稿存在 {item['reason']} 问题，"
            "请补全标题和正文，并确保正文不少于300字。"
        )
        style_hints = "\n".join(part for part in [base_style_hints, reflection] if part)
        templates = await _templates_for_category(
            state,
            template_repository,
            article.get("category_v2", ""),
        )
        generated = await draft_gen.generate(
            article,
            scores,
            style_hints=style_hints,
            templates=templates,
            system_prompt_template=system_prompt_template,
        )
        if not generated.get("ok") or not generated.get("drafts"):
            continue
        replacement = next(
            (draft for draft in generated["drafts"] if draft.get("index") == item["index"]),
            generated["drafts"][0],
        )
        await db["user_drafts"].update_one(
            {
                "user_id": state["user_id"],
                "article_url_hash": item["url_hash"],
            },
            {"$set": {f"drafts.{item['position']}": replacement}},
        )
        rewritten_count += 1

    state["rewritten_count"] = rewritten_count
    logger.info("[rewrite] Rewritten %d drafts", rewritten_count)
    return state


async def review_node(state: dict, reviewer: Any, db: Any) -> dict:
    """并发检查最终稿并把结果写入对应 drafts 数组位置。"""

    from agent.pipeline import _log_stage

    started = time.perf_counter()
    state["current_phase"] = PipelinePhaseV2.REVIEW.value
    state["review_count"] = 0
    state["review_failed_count"] = 0
    state["review_reused_count"] = 0
    await _log_stage(state, db, "review", "draft review started", action="start")

    if db is None:
        await _log_stage(state, db, "review", "draft review skipped: no database", action="skip")
        return state
    if reviewer is None:
        state["errors"].append("review: reviewer not initialized")
        await _log_stage(
            state,
            db,
            "review",
            "draft reviewer not initialized; failed statuses will be stored",
            action="degraded",
        )

    cursor = db["user_drafts"].find({"user_id": state["user_id"]})
    documents = await cursor.to_list(length=30)
    jobs: list[tuple[str, int, dict[str, Any], dict[str, Any]]] = []
    reused_count = 0
    for document in documents:
        url_hash = str(document.get("article_url_hash") or "")
        article = await db["articles"].find_one({"url_hash": url_hash}) or {}
        for position, draft in enumerate(document.get("drafts", [])):
            current_hash = compute_content_hash(str(draft.get("content_md") or ""))
            existing_review = draft.get("review") or {}
            if (
                existing_review.get("status") == "completed"
                and existing_review.get("content_hash") == current_hash
            ):
                reused_count += 1
                continue
            jobs.append((url_hash, position, article, draft))

    semaphore = asyncio.Semaphore(2)

    async def review_one(
        url_hash: str,
        position: int,
        article: dict[str, Any],
        draft: dict[str, Any],
    ) -> str:
        async with semaphore:
            try:
                if reviewer is None:
                    raise RuntimeError("Draft reviewer not initialized")
                result = await reviewer.review(article, draft)
            except Exception as exc:
                logger.warning("[review] Draft review failed for %s/%d: %s", url_hash, position, exc)
                result = DraftReview(
                    status="failed",
                    content_hash=compute_content_hash(str(draft.get("content_md") or "")),
                    summary="稿件检查失败",
                    issues=[],
                    counts={"high": 0, "medium": 0, "low": 0},
                    fact_check_available=bool(article.get("content_md")),
                    error=str(exc).strip() or type(exc).__name__,
                )
        await db["user_drafts"].update_one(
            {"user_id": state["user_id"], "article_url_hash": url_hash},
            {
                "$set": {
                    f"drafts.{position}.review": result.model_dump(mode="json"),
                    "updated_at": datetime.now(UTC),
                }
            },
        )
        return result.status

    statuses = await asyncio.gather(*(review_one(*job) for job in jobs)) if jobs else []
    state["review_count"] = len(statuses)
    state["review_failed_count"] = sum(status == "failed" for status in statuses)
    state["review_reused_count"] = reused_count
    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "[review] Reviewed %d drafts, reused %d, failed %d",
        state["review_count"],
        reused_count,
        state["review_failed_count"],
    )
    await _log_stage(
        state,
        db,
        "review",
        f"reviewed {state['review_count']} drafts, reused {reused_count}",
        duration_ms=duration_ms,
        detail={
            "review_count": state["review_count"],
            "review_failed_count": state["review_failed_count"],
            "review_reused_count": reused_count,
        },
    )
    return state


async def _load_custom_system_prompt(db: Any, user_id: str) -> str | None:
    """Load an optional user override and safely fall back when unavailable."""
    try:
        from api.user_prompts import DRAFT_SYSTEM_PROMPT_KEY, get_effective_prompt

        prompt = await get_effective_prompt(db, user_id, DRAFT_SYSTEM_PROMPT_KEY)
    except Exception as exc:
        logger.warning("[draft] Failed to load custom prompt for user_id=%s: %s", user_id, exc)
        return None
    if not prompt.is_custom:
        return None
    logger.info("[draft] 使用自定义提示词: %s", user_id)
    return prompt.content


async def _templates_for_category(
    state: dict,
    template_repository: Any,
    category_v2: str,
) -> list[EffectivePRTemplate] | None:
    """Return task-frozen templates, resolving and caching only for standalone node calls."""
    if template_repository is None:
        return None
    frozen = state.setdefault("frozen_templates", {})
    documents = frozen.get(category_v2)
    if documents is None:
        resolved = await template_repository.resolve(state["user_id"], category_v2)
        documents = [template.model_dump(mode="json") for template in resolved]
        frozen[category_v2] = documents
    return [EffectivePRTemplate.model_validate(document) for document in documents]


def _template_log_metadata(templates: list[EffectivePRTemplate]) -> list[dict[str, Any]]:
    """Return identifiers safe for execution logs; template content is deliberately excluded."""
    return [
        {
            "template_key": str(template.template_key),
            "template_id": template.template_id,
            "version": template.version,
            "source": str(template.source),
        }
        for template in templates
    ]


async def _load_style_hints(db: Any, user_id: str) -> str | None:
    """读取用户画像并转换为草稿生成可注入的风格提示。"""
    from agent.style_profiler import load_style_hints

    return await load_style_hints(db, user_id)


def route_after_crawl(state: dict) -> str:
    """Route incomplete articles through one enrichment pass."""
    return "enrich" if state.get("needs_enrich") and not state.get("enriched") else "classify_v2"


def route_after_score(state: dict) -> str:
    """Retry anomalous scores once, then continue to draft generation."""
    return "score_v2" if state.get("score_anomaly") and not state.get("score_retried") else "draft"


def route_after_quality_check(state: dict) -> str:
    """Route low-quality drafts through one reflection rewrite pass."""
    return "rewrite" if state.get("needs_rewrite") else "review"


# ═══════════════════════════════════════════════════════════════
# PipelineManagerV2
# ═══════════════════════════════════════════════════════════════


class PipelineManagerV2:
    """V2 流水线生命周期管理器。

    V2 流水线:
      crawl → [enrich] → classify_v2 → filter → score_v2 → draft
      → quality_check → [rewrite] → review
    """

    def __init__(
        self,
        tools: dict,
        classifier_v2: Any,  # ClassifierV2
        scorer_v2: Any,  # ScoringAgentV2
        draft_gen: Any,  # DraftGenerator
        knowledge: Any,  # KnowledgeLoader
        db: Any = None,  # AsyncIOMotorDatabase
        crawl_client: McpCrawlClient | None = None,
        template_repository: Any = None,
        reviewer: Any = None,
    ):
        self.tools = tools
        self.classifier_v2 = classifier_v2
        self.scorer_v2 = scorer_v2
        self.draft_gen = draft_gen
        self.knowledge = knowledge
        self.db = db
        self.crawl_client = crawl_client
        self.template_repository = template_repository
        self.reviewer = reviewer
        self._state_manager = PipelineStateManager(db) if db is not None else None
        self._graph = self._build_graph()

    # ── 公开接口 ──────────────────────────────────────────────

    async def run_full(
        self,
        crawl_days: int = 1,
        user_id: str = "",
        trace_id: str = "",
        username: str = "",
        task_id: str = "",
        request_id: str = "",
    ) -> dict:
        """执行全流程 V2 流水线，状态按 task_id 持久化。"""
        return await self._run(crawl_days, user_id, trace_id, username, task_id, request_id)

    # ── 内部实现 ──────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(dict)
        ctools = self.tools
        cdb = self.db
        cclassifier = self.classifier_v2
        cscorer = self.scorer_v2
        cdraft_gen = self.draft_gen
        cknowledge = self.knowledge
        crawl_client = self.crawl_client
        template_repository = self.template_repository
        reviewer = self.reviewer

        async def _crawl(state: dict) -> dict:
            return await crawl_node_v2(state, ctools, cdb, crawl_client)

        async def _classify_v2(state: dict) -> dict:
            return await classify_v2_node(state, cclassifier, cdb)

        async def _enrich(state: dict) -> dict:
            return await enrich_node(state, ctools, cdb, crawl_client)

        async def _filter(state: dict) -> dict:
            return await filter_node(state, cdb)

        async def _score_v2(state: dict) -> dict:
            return await score_v2_node(state, cscorer, cknowledge, cdb)

        async def _draft(state: dict) -> dict:
            return await draft_node(
                state,
                cdraft_gen,
                cknowledge,
                cdb,
                template_repository,
            )

        async def _quality_check(state: dict) -> dict:
            return await quality_check_node(state, cdb)

        async def _rewrite(state: dict) -> dict:
            return await rewrite_node(
                state,
                cdraft_gen,
                cknowledge,
                cdb,
                template_repository,
            )

        async def _review(state: dict) -> dict:
            return await review_node(state, reviewer, cdb)

        graph.add_node("crawl", _crawl)
        graph.add_node("enrich", _enrich)
        graph.add_node("classify_v2", _classify_v2)
        graph.add_node("filter", _filter)
        graph.add_node("score_v2", _score_v2)
        graph.add_node("draft", _draft)
        graph.add_node("quality_check", _quality_check)
        graph.add_node("rewrite", _rewrite)
        graph.add_node("review", _review)

        graph.set_entry_point("crawl")
        graph.add_conditional_edges(
            "crawl",
            route_after_crawl,
            {"enrich": "enrich", "classify_v2": "classify_v2"},
        )
        graph.add_edge("enrich", "classify_v2")
        graph.add_edge("classify_v2", "filter")
        graph.add_edge("filter", "score_v2")
        graph.add_conditional_edges(
            "score_v2",
            route_after_score,
            {"score_v2": "score_v2", "draft": "draft"},
        )
        graph.add_edge("draft", "quality_check")
        graph.add_conditional_edges(
            "quality_check",
            route_after_quality_check,
            {"rewrite": "rewrite", "review": "review"},
        )
        graph.add_edge("rewrite", "review")
        graph.add_edge("review", END)

        checkpointer = None
        if supports_mongodb_checkpoints(self.db):
            checkpointer = create_checkpointer(self.db)
        elif self.db is not None:
            logger.debug("MongoDB checkpointer disabled for non-Motor database")
        return graph.compile(checkpointer=checkpointer)

    async def _run(
        self,
        crawl_days: int,
        user_id: str,
        trace_id: str = "",
        username: str = "",
        task_id: str = "",
        request_id: str = "",
    ) -> dict:
        pipeline_id = task_id or uuid4().hex[:8]
        state = create_state_v2(
            crawl_days=crawl_days,
            user_id=user_id,
            trace_id=trace_id,
            username=username,
            request_id=request_id,
        )
        state["task_id"] = pipeline_id
        state["status"] = PipelineStatusV2.RUNNING.value
        state["started_at"] = _now_iso()

        if self._state_manager is not None:
            task = await self._state_manager.get_task(pipeline_id, user_id)
            if task is None:
                await self._state_manager.create_task(
                    pipeline_id,
                    user_id,
                    "run-v2",
                    crawl_days=crawl_days,
                    trace_id=trace_id,
                    username=username,
                )
            await self._state_manager.update_status(
                pipeline_id,
                PipelineStatusV2.RUNNING.value,
                progress={
                    "phase": "crawl",
                    "current": 0,
                    "total": 9,
                    "message": "正在启动流水线...",
                },
                state=dict(state),
                task_metadata={
                    "thread_id": f"thread-{pipeline_id}",
                    "checkpoint_ns": "",
                    "crawl_days": crawl_days,
                    "trace_id": trace_id or None,
                    "username": username or user_id,
                },
            )

        try:
            if self.template_repository is not None:
                templates = await self.template_repository.list_effective_templates(user_id)
                grouped: dict[str, list[dict[str, Any]]] = {}
                for template in templates:
                    grouped.setdefault(str(template.category_v2), []).append(
                        template.model_dump(mode="json")
                    )
                state["frozen_templates"] = grouped
            config = {"configurable": {"thread_id": f"thread-{pipeline_id}"}}
            final_state = await self._graph.ainvoke(state, config=config)
            final_state["status"] = PipelineStatusV2.COMPLETED.value
            final_state["finished_at"] = _now_iso()
        except asyncio.CancelledError:
            final_state = state
            final_state["status"] = PipelineStatusV2.CANCELLED.value
            final_state["finished_at"] = _now_iso()
        except Exception as e:
            logger.error("[pipeline_v2] Fatal: %s", e)
            final_state = state
            final_state["status"] = PipelineStatusV2.FAILED.value
            final_state["errors"].append(f"fatal: {e}")
            final_state["finished_at"] = _now_iso()

        status = final_state["status"]
        result = {
            "pipeline_id": pipeline_id,
            "status": status,
            "state": dict(final_state),
        }
        if self._state_manager is not None:
            await self._state_manager.update_status(
                pipeline_id,
                status,
                progress={
                    "phase": status,
                    "current": 9 if status == PipelineStatusV2.COMPLETED.value else 0,
                    "total": 9,
                    "message": "任务完成"
                    if status == PipelineStatusV2.COMPLETED.value
                    else "任务已结束",
                },
                last_node=final_state.get("current_phase", ""),
                state=dict(final_state),
                error="; ".join(final_state["errors"]) if final_state["errors"] else None,
                result=result,
            )

        logger.info(
            "[pipeline_v2] %s — crawled=%d classified=%d pr_eligible=%d scored=%d drafts=%d",
            status,
            final_state["crawled_count"],
            final_state["classified_v2_count"],
            final_state.get("pr_eligible_count", 0),
            final_state["scored_v2_count"],
            final_state["draft_count"],
        )
        return result

    async def resume_from_checkpoint(self, task_id: str) -> dict:
        """Resume a task from its latest durable LangGraph checkpoint."""
        config = {"configurable": {"thread_id": f"thread-{task_id}"}}
        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            error = "No checkpoint found"
            if self._state_manager is not None:
                await self._state_manager.update_status(task_id, "failed", error=error)
            return {"pipeline_id": task_id, "status": "failed", "error": error}

        if self._state_manager is not None:
            await self._state_manager.update_status(
                task_id,
                "running",
                progress={
                    "phase": "resume",
                    "current": 0,
                    "total": 9,
                    "message": "正在从检查点恢复...",
                },
            )

        final_state = (
            await self._graph.ainvoke(None, config=config)
            if snapshot.next
            else dict(snapshot.values)
        )
        final_state["status"] = PipelineStatusV2.COMPLETED.value
        final_state["finished_at"] = _now_iso()
        result = {
            "pipeline_id": task_id,
            "status": PipelineStatusV2.COMPLETED.value,
            "state": dict(final_state),
        }
        if self._state_manager is not None:
            await self._state_manager.update_status(
                task_id,
                PipelineStatusV2.COMPLETED.value,
                progress={
                    "phase": "completed",
                    "current": 9,
                    "total": 9,
                    "message": "任务恢复完成",
                },
                last_node=final_state.get("current_phase", ""),
                state=dict(final_state),
                result=result,
            )
        return result


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")
