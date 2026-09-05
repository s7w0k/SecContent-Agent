"""ARQ task definitions and Redis queue configuration."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from agent.pipeline_state import PipelineStateManager
from arq.connections import RedisSettings
from arq.cron import cron
from arq.worker import Retry, func
from config import get_settings

logger = logging.getLogger("backend.agent.task_queue")


def redis_settings() -> RedisSettings:
    """Build ARQ Redis settings from the central application configuration."""
    settings = get_settings()
    return RedisSettings(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        database=settings.REDIS_DB,
        password=settings.REDIS_PASSWORD or None,
    )


async def execute_pipeline(
    ctx: dict[str, Any],
    task_id: str,
    user_id: str,
    task_type: str,
    crawl_days: int = 1,
    article_url_hash: str | None = None,
    trace_id: str = "",
    username: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """Execute one persisted pipeline task inside the worker process."""
    state_manager = PipelineStateManager(ctx["db"])
    task = await state_manager.get_task(task_id, user_id)
    if task is None:
        logger.warning("Queued pipeline task not found: task_id=%s user_id=%s", task_id, user_id)
        return {"status": "failed", "error": "task not found"}

    # Refresh knowledge at task boundary
    from agent.knowledge_runtime import KnowledgeRuntimeRefresher

    app = ctx.get("app")
    app_state = getattr(app, "state", None) if app is not None else None
    knowledge_hash = ""
    if app_state is not None:
        refresher = KnowledgeRuntimeRefresher(app_state)
        knowledge_hash = await refresher.prepare_for_task()
        if knowledge_hash:
            logger.info("Task starting with knowledge hash: %s", knowledge_hash[:8])

    try:
        # Cutover：统一经由 ExecutionRouter（PR-1 §61），避免反向依赖 api.pipeline。
        from agent.execution.contracts import ExecutionRequest

        router = ctx.get("execution_router")
        if router is None:
            raise RuntimeError("execution_router not configured in worker ctx")
        request = ExecutionRequest(
            task_id=task_id,
            task_type=task_type,
            goal="",
            user_id=user_id,
            tenant_id=user_id,
            trace_id=trace_id,
            request_id=request_id,
            crawl_days=crawl_days,
            article_url_hash=article_url_hash,
            username=username,
            execution_mode=get_settings().AGENT_EXECUTION_MODE,
        )
        # Retry sticky（§32 / §48）：任务已选 engine 则复用；否则首跑选定并持久化，
        # 使失败重试仍走同一 engine，避免 ARQ 重跑被重新 rollout。
        selected_engine = task.get("selected_engine")
        if selected_engine is None:
            selected_engine = router.select_engine(request)
            try:
                await state_manager.update_status(
                    task_id,
                    task.get("status") or "running",
                    task_metadata={
                        "selected_engine": selected_engine,
                        "execution_mode": request.execution_mode,
                    },
                )
            except Exception:
                logger.warning(
                    "persist selected_engine failed; default to mode selection", exc_info=True
                )
        request.selected_engine = selected_engine
        logger.info("task selected_engine=%s task_id=%s", request.selected_engine, task_id)
        await router.execute(request)
    except Exception as exc:
        retry_count = await state_manager.increment_retry(task_id)
        max_retries = get_settings().ARQ_MAX_RETRIES
        if retry_count <= max_retries:
            logger.warning(
                "Pipeline task retry scheduled: task_id=%s retry=%d/%d error=%s",
                task_id,
                retry_count,
                max_retries,
                exc,
            )
            raise Retry(defer=30) from exc
        raise

    completed = await state_manager.get_task(task_id, user_id)
    return {
        "task_id": task_id,
        "status": completed.get("status", "completed") if completed else "completed",
    }


async def fetch_fulltext_batch(
    ctx: dict[str, Any],
    articles: list[dict[str, str]],
    trace_id: str = "",
    user_id: str = "",
    request_id: str = "",
) -> dict[str, int]:
    """Run the existing overseas full-text enrichment helper in ARQ."""
    from agent.pipeline import _fetch_fulltext_background

    await _fetch_fulltext_background(
        ctx["db"],
        articles,
        trace_id,
        client=ctx["mcp_crawl_client"],
        user_id=user_id,
        request_id=request_id,
    )
    return {"requested": len(articles)}


async def enrich_web_search_articles(
    ctx: dict[str, Any],
    article_url_hashes: list[str],
    trace_id: str = "",
    user_id: str = "",
) -> dict[str, int]:
    """Fetch full text for web search articles that have empty content_md."""
    import httpx
    from bs4 import BeautifulSoup
    from utils.url_safety import is_safe_url

    db = ctx["db"]
    success = 0
    failed = 0
    skipped = 0

    for url_hash in article_url_hashes:
        article = await db["articles"].find_one({"url_hash": url_hash})
        if not article:
            skipped += 1
            continue

        # Skip if already has content
        if article.get("content_md"):
            skipped += 1
            continue

        url = article.get("url", "")
        if not url or not is_safe_url(url):
            await db["articles"].update_one(
                {"url_hash": url_hash},
                {
                    "$set": {
                        "content_fetch_status": "blocked",
                        "content_fetch_error": "URL不安全",
                    }
                },
            )
            failed += 1
            continue

        # Mark as fetching
        await db["articles"].update_one(
            {"url_hash": url_hash},
            {"$set": {"content_fetch_status": "fetching"}},
        )

        try:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                max_redirects=5,
                headers={"User-Agent": "PR-Agent-Fetch/1.0"},
            ) as client:
                resp = await client.get(url)
                content_type = resp.headers.get("content-type", "")
                if not any(
                    t in content_type for t in ("text/html", "text/plain", "application/xhtml")
                ):
                    await db["articles"].update_one(
                        {"url_hash": url_hash},
                        {
                            "$set": {
                                "content_fetch_status": "blocked",
                                "content_fetch_error": f"不支持的内容类型: {content_type}",
                            }
                        },
                    )
                    failed += 1
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                content = soup.get_text(separator="\n", strip=True)

            if not content:
                await db["articles"].update_one(
                    {"url_hash": url_hash},
                    {
                        "$set": {
                            "content_fetch_status": "failed",
                            "content_fetch_error": "内容为空",
                        }
                    },
                )
                failed += 1
                continue

            await db["articles"].update_one(
                {"url_hash": url_hash},
                {
                    "$set": {
                        "content_md": content[:50000],
                        "content_fetch_status": "completed",
                        "content_fetch_error": None,
                        "pipeline_status": "ready",
                    }
                },
            )
            success += 1
        except httpx.TimeoutException:
            await db["articles"].update_one(
                {"url_hash": url_hash},
                {
                    "$set": {
                        "content_fetch_status": "failed",
                        "content_fetch_error": "抓取超时",
                    }
                },
            )
            failed += 1
        except Exception as e:
            await db["articles"].update_one(
                {"url_hash": url_hash},
                {
                    "$set": {
                        "content_fetch_status": "failed",
                        "content_fetch_error": str(e)[:200],
                    }
                },
            )
            failed += 1

    logger.info(
        "Web search enrichment done: success=%d failed=%d skipped=%d",
        success,
        failed,
        skipped,
    )
    return {"success": success, "failed": failed, "skipped": skipped}


async def resume_pipeline(
    ctx: dict[str, Any],
    task_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Resume a V2 task from its latest LangGraph checkpoint in the worker."""
    state_manager = PipelineStateManager(ctx["db"])
    task = await state_manager.get_task(task_id, user_id)
    if task is None:
        logger.warning("Resume task not found: task_id=%s user_id=%s", task_id, user_id)
        return {"status": "failed", "error": "task not found"}

    try:
        # Cutover：Resume 统一经由 ExecutionRouter（§31-32 / §65），读取任务创建时
        # 选定的 engine，sticky 复用，不重新 rollout；消除对 worker 上下文中旧 DAG 的直接依赖。
        from agent.execution.contracts import ExecutionRequest

        router = ctx.get("execution_router")
        if router is None:
            raise RuntimeError("execution_router not configured in worker ctx")
        resume_request = ExecutionRequest(
            task_id=task_id,
            task_type="run-v2",
            goal="",
            user_id=user_id,
            tenant_id=user_id,
        )
        # Resume sticky（§33 / §49）：读取任务创建时选定的 engine，不重新 rollout；
        # 历史任务没有 selected_engine → router 回退到 legacy（§49）。
        resume_request.selected_engine = task.get("selected_engine")
        result = await router.resume(resume_request)
        if result.status == "FAILED":
            raise RuntimeError(result.error_message or "checkpoint resume failed")
        return {
            "task_id": task_id,
            "pipeline_id": task_id,
            "status": "completed",
            "engine": result.engine,
        }
    except Exception as exc:
        await state_manager.update_status(task_id, "failed", error=str(exc))
        retry_count = await state_manager.increment_retry(task_id)
        max_retries = get_settings().ARQ_MAX_RETRIES
        if retry_count <= max_retries:
            logger.warning(
                "Pipeline resume retry scheduled: task_id=%s retry=%d/%d error=%s",
                task_id,
                retry_count,
                max_retries,
                exc,
            )
            raise Retry(defer=30) from exc
        raise


_settings = get_settings()


async def process_memory_event(
    ctx: dict[str, Any],
    event_id: str,
    user_id: str,
) -> dict[str, Any]:
    """ARQ 任务：处理单个记忆事件，提取候选偏好。

    从 user_memory_events 读取事件，调用 MemoryLearner 提取候选，
    写入 user_memory_items。
    """
    from agent.memory_learner import MemoryLearner

    db = ctx.get("db")
    if db is None:
        logger.warning("process_memory_event: db not available, skipping")
        return {"ok": False, "error": "db not available"}

    event = await db["user_memory_events"].find_one({"event_id": event_id})
    if event is None:
        logger.warning("process_memory_event: event not found: %s", event_id)
        return {"ok": False, "error": "event not found"}

    # 已完成或已跳过的事件直接返回（幂等）
    if event.get("status") in ("completed", "skipped"):
        return {"ok": True, "already_processed": True}

    llm = ctx.get("llm")
    if llm is None:
        logger.warning("process_memory_event: llm not available, skipping")
        return {"ok": False, "error": "llm not available"}

    learner = MemoryLearner(llm=llm, db=db)
    result = await learner.process_event(db, event)
    return result


async def compile_memory_summaries(
    ctx: dict[str, Any],
    user_id: str,
    scope_keys: list[str] | None = None,
) -> dict[str, Any]:
    """ARQ 任务：编译用户记忆摘要。"""
    from agent.memory_compiler import MemorySummaryCompiler

    db = ctx.get("db")
    if db is None:
        return {"ok": False, "error": "db not available"}

    compiler = MemorySummaryCompiler(db)
    result = await compiler.compile_user(user_id)
    return result


async def decay_user_memories(ctx: dict[str, Any]) -> dict[str, Any]:
    """ARQ 定时任务：更新时间衰减，过期低置信度记忆。"""
    from agent.memory_confidence import _time_decay
    from config import get_settings as _gs

    db = ctx.get("db")
    if db is None:
        return {"ok": False, "error": "db not available"}

    settings = _gs()
    now = datetime.now(UTC)

    # 查询所有 active 和 pending_approval 的非用户确认记忆
    cursor = db["user_memory_items"].find(
        {
            "status": {"$in": ["active", "pending_approval", "candidate"]},
            "confirmed_by_user": False,
        }
    )
    items = await cursor.to_list(length=10000)

    expired_count = 0
    for item in items:
        last_seen = item.get("last_seen_at")
        if not isinstance(last_seen, datetime):
            continue

        decay = _time_decay(last_seen, settings.MEMORY_DECAY_HALF_LIFE_DAYS)
        current_confidence = item.get("confidence", 0.0)
        decayed_confidence = current_confidence * decay

        if decayed_confidence < settings.MEMORY_PENDING_THRESHOLD:
            await db["user_memory_items"].update_one(
                {"memory_id": item["memory_id"]},
                {"$set": {"status": "expired", "updated_at": now}},
            )
            expired_count += 1

    logger.info("decay_user_memories: processed=%d expired=%d", len(items), expired_count)
    return {"ok": True, "processed": len(items), "expired": expired_count}


async def cleanup_expired_articles(ctx: dict[str, Any]) -> dict:
    """定时清理过期文章。

    规则（按入库时间 added_at 计算）：
    - 超过 48 小时的"不相关"文章 → 删除
    - 超过 14 天的所有文章 → 删除
    """
    db = ctx.get("db")
    if db is None:
        logger.warning("cleanup_expired_articles: db not available in ctx, skipping")
        return {"ok": False, "error": "db not available"}

    now = datetime.now(UTC)

    # 48 小时前的"不相关"文章
    cutoff_48h = now - timedelta(hours=48)
    result_irrelevant = await db["articles"].delete_many(
        {
            "added_at": {"$lt": cutoff_48h},
            "category_v2": "不相关",
        }
    )

    # 14 天前的所有文章
    cutoff_14d = now - timedelta(days=14)
    result_all = await db["articles"].delete_many(
        {
            "added_at": {"$lt": cutoff_14d},
        }
    )

    logger.info(
        "cleanup_expired_articles: deleted %d irrelevant (>48h), %d expired (>14d)",
        result_irrelevant.deleted_count,
        result_all.deleted_count,
    )
    return {
        "ok": True,
        "deleted_irrelevant": result_irrelevant.deleted_count,
        "deleted_expired": result_all.deleted_count,
    }


# ── 16.5: ARQ 每日定时海外新闻抓取 ──────────────────────


async def _auto_classify_and_score(
    *,
    ctx: dict[str, Any],
    db,
    run_id: str,
    trace_id: str,
    settings,
) -> dict[str, Any]:
    """对本次抓取的新文章自动执行分类 + 打分。

    Returns:
        {"classified": int, "scored": int, "pr_candidates": int, "errors": dict}
    """
    import asyncio

    result = {"classified": 0, "scored": 0, "pr_candidates": 0, "errors": {}}

    # 从 worker 上下文获取分类器和打分器
    app = ctx.get("app")
    if app is None:
        logger.warning("[overseas-schedule] app not in ctx, skip classify+score")
        result["errors"]["classify"] = "app not available"
        return result

    classifier = getattr(app.state, "classifier_v2", None)
    scorer = getattr(app.state, "scorer_v2", None)
    if classifier is None or scorer is None:
        logger.warning("[overseas-schedule] classifier/scorer not available, skip")
        result["errors"]["classify"] = "classifier or scorer not available"
        return result

    # 1. 查询本次抓取的未分类文章
    articles = (
        await db["articles"]
        .find(
            {
                "crawl_run_id": run_id,
                "$or": [
                    {"category_v2": {"$in": ["", None]}},
                    {"category_v2": {"$exists": False}},
                ],
            }
        )
        .to_list(length=500)
    )

    if not articles:
        logger.info("[overseas-schedule] no new articles to classify")
        return result

    logger.info(
        "[overseas-schedule] auto-classifying %d articles for run_id=%s",
        len(articles),
        run_id,
    )

    # 2. 并发分类（并发度 5）
    semaphore = asyncio.Semaphore(5)

    async def classify_one(article: dict[str, Any]) -> dict[str, Any] | None:
        async with semaphore:
            try:
                return await classifier.classify_single(
                    article,
                    user_id="system:scheduler",
                    trace_id=trace_id,
                    task_id=run_id,
                )
            except Exception as exc:
                logger.warning(
                    "[overseas-schedule] classify failed for %s: %s",
                    article.get("url_hash", ""),
                    exc,
                )
                return None

    futures = [asyncio.create_task(classify_one(a)) for a in articles]
    classified_articles: list[dict[str, Any]] = []
    for future in asyncio.as_completed(futures):
        cls_result = await future
        if cls_result is not None:
            # 查找原始文章并更新
            for art in articles:
                if art.get("url_hash") == getattr(cls_result, "url_hash", None):
                    try:
                        update_fields = {
                            "category_v2": cls_result.category,
                            "category_v2_confidence": cls_result.confidence,
                            "category_v2_reason": cls_result.reason,
                            "category_v2_fallback": cls_result.fallback,
                            "is_pr_eligible": cls_result.is_pr_eligible,
                            "is_ai_agent_security_relevant": cls_result.is_ai_agent_security_relevant,
                            "ai_agent_security_relevance_confidence": cls_result.ai_agent_security_relevance_confidence,
                            "ai_agent_security_relevance_reason": cls_result.ai_agent_security_relevance_reason,
                            "pipeline_status": "classified",
                        }
                        await db["articles"].update_one(
                            {"_id": art["_id"]},
                            {"$set": update_fields},
                        )
                        result["classified"] += 1
                        # 传递分类结果给打分阶段
                        art.update(update_fields)
                        if cls_result.is_pr_eligible:
                            classified_articles.append(art)
                    except Exception as exc:
                        logger.warning("[overseas-schedule] DB update failed: %s", exc)
                    break

    logger.info(
        "[overseas-schedule] classified %d/%d, %d PR-eligible",
        result["classified"],
        len(articles),
        len(classified_articles),
    )

    # 3. 对 PR-eligible 文章打分
    if not classified_articles:
        logger.info("[overseas-schedule] no PR-eligible articles to score")
        return result

    logger.info(
        "[overseas-schedule] auto-scoring %d PR-eligible articles for run_id=%s",
        len(classified_articles),
        run_id,
    )

    async def score_one(article: dict[str, Any]) -> dict[str, Any] | None:
        async with semaphore:
            try:
                return await scorer.score_single(
                    article,
                    user_id="system:scheduler",
                    trace_id=trace_id,
                    task_id=run_id,
                )
            except Exception as exc:
                logger.warning(
                    "[overseas-schedule] score failed for %s: %s", article.get("url_hash", ""), exc
                )
                return None

    score_futures = [asyncio.create_task(score_one(a)) for a in classified_articles]
    for future in asyncio.as_completed(score_futures):
        score_result = await future
        if score_result is not None and not score_result.get("_fallback", True):
            try:
                await db["articles"].update_one(
                    {"url_hash": score_result.get("url_hash", "")},
                    {
                        "$set": {
                            "product_relevance": score_result["product_relevance"],
                            "event_impact": score_result["event_impact"],
                            "pr_total_score": score_result["pr_total_score"],
                            "score_reason": score_result.get("score_reason", ""),
                            "product_scores": score_result.get("product_scores", []),
                            "pipeline_status": "scored",
                        }
                    },
                )
                result["scored"] += 1
                if score_result.get("is_pr_candidate", False):
                    result["pr_candidates"] += 1
            except Exception as exc:
                logger.warning("[overseas-schedule] score DB update failed: %s", exc)

    logger.info(
        "[overseas-schedule] scored %d/%d, %d PR candidates",
        result["scored"],
        len(classified_articles),
        result["pr_candidates"],
    )

    return result


async def scheduled_overseas_news_crawl(ctx: dict[str, Any]) -> dict[str, Any]:
    """ARQ 定时任务：每日北京时间 07:00 抓取海外新闻。

    处理流程：
    1. 计算业务日期
    2. 原子声明每日 run（幂等）
    3. 获取共享锁
    4. 调用统一入库服务
    5. 更新 crawl_runs 结果
    6. 释放锁
    7. 可恢复错误触发 Retry
    """
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from agent.overseas_news_service import CrawlServiceError, OverseasNewsIngestionService
    from models.crawl_run import (
        CrawlRunStatus,
        build_lock_key,
        build_run_id,
        build_run_key,
        create_initial_run,
    )

    settings = get_settings()
    tz = ZoneInfo(settings.OVERSEAS_NEWS_SCHEDULE_TIMEZONE)
    local_now = datetime.now(tz)
    schedule_date = local_now.date().isoformat()
    run_key = build_run_key("scheduled", schedule_date)
    run_id = build_run_id(schedule_date)
    lock_key = build_lock_key(schedule_date)
    trace_id = f"scheduler:{run_id}"
    request_id = f"scheduler:{run_id}"

    db = ctx.get("db")
    if db is None:
        logger.error("[overseas-schedule] db not available in ctx")
        return {"ok": False, "error": "DATABASE_UNAVAILABLE"}

    # 1. 原子声明每日 run
    now = datetime.now(UTC)
    initial_doc = create_initial_run(
        run_id=run_id,
        run_key=run_key,
        trigger="scheduled",
        actor_id="system:scheduler",
        schedule_date=schedule_date,
        timezone=settings.OVERSEAS_NEWS_SCHEDULE_TIMEZONE,
        crawl_days=settings.OVERSEAS_NEWS_SCHEDULE_CRAWL_DAYS,
        trace_id=trace_id,
        lock_key=lock_key,
        retention_days=settings.OVERSEAS_NEWS_RUN_RETENTION_DAYS,
    )

    # 移除与 $set 冲突的字段（MongoDB 不允许同一字段同时出现在 $set 和 $setOnInsert）
    for field in ("status", "started_at", "updated_at", "run_id", "expires_at", "attempt"):
        initial_doc.pop(field, None)

    try:
        from pymongo import ReturnDocument

        await db["crawl_runs"].find_one_and_update(
            {
                "run_key": run_key,
                "status": {"$in": ["pending", "failed"]},
            },
            {
                "$set": {
                    "status": CrawlRunStatus.RUNNING.value,
                    "started_at": now,
                    "updated_at": now,
                    "run_id": run_id,
                },
                "$inc": {"attempt": 1},
                "$setOnInsert": initial_doc,
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except Exception as exc:
        logger.warning("[overseas-schedule] run_key=%s upsert failed: %s", run_key, exc)
        return {"ok": True, "status": "skipped", "reason": "daily_run_exists"}

    # 2. 获取共享锁
    lock_expires = now + timedelta(seconds=settings.OVERSEAS_NEWS_LOCK_TTL_SECONDS)
    try:
        await db["pipeline_locks"].update_one(
            {"lock_key": lock_key, "status": {"$in": [None, "completed", "failed", "expired"]}},
            {
                "$set": {
                    "lock_key": lock_key,
                    "lock_type": "crawl",
                    "source": "overseas_news",
                    "status": "running",
                    "user_id": "system:scheduler",
                    "run_id": run_id,
                    "created_at": now,
                    "expires_at": lock_expires,
                }
            },
            upsert=True,
        )
    except Exception:
        logger.info("[overseas-schedule] lock_key=%s held by another run", lock_key)
        await db["crawl_runs"].update_one(
            {"run_key": run_key},
            {"$set": {"status": CrawlRunStatus.SKIPPED.value, "updated_at": now}},
        )
        return {"ok": True, "status": "skipped", "reason": "lock_held"}

    # 3. 调用统一入库服务
    app = ctx.get("app")
    tools = getattr(app.state, "pipeline_manager", None) if app else None
    if tools and hasattr(tools, "tools"):
        crawl_tools = tools.tools
    elif ctx.get("pipeline_v2"):
        crawl_tools = ctx["pipeline_v2"].tools
    else:
        crawl_tools = None

    if crawl_tools is None:
        logger.error("[overseas-schedule] tools not available")
        await db["crawl_runs"].update_one(
            {"run_key": run_key},
            {"$set": {"status": CrawlRunStatus.FAILED.value, "updated_at": now}},
        )
        return {"ok": False, "error": "TOOLS_UNAVAILABLE"}

    arq_pool = ctx.get("redis")

    service = OverseasNewsIngestionService(
        db=db,
        tools=crawl_tools,
        arq_pool=arq_pool,
    )

    try:
        result = await service.run(
            crawl_days=settings.OVERSEAS_NEWS_SCHEDULE_CRAWL_DAYS,
            trigger="scheduled",
            actor_id="system:scheduler",
            trace_id=trace_id,
            request_id=request_id,
            run_id=run_id,
        )

        # 4. 更新 crawl_runs 结果
        finished = datetime.now(UTC)
        from models.crawl_run import CrawlRun

        run = CrawlRun(
            run_key=run_key,
            run_id=run_id,
            trigger="scheduled",
            actor_id="system:scheduler",
            schedule_date=schedule_date,
            timezone=settings.OVERSEAS_NEWS_SCHEDULE_TIMEZONE,
            crawl_days=settings.OVERSEAS_NEWS_SCHEDULE_CRAWL_DAYS,
            status=CrawlRunStatus(result["status"]),
            trace_id=trace_id,
            lock_key=lock_key,
            started_at=now,
            finished_at=finished,
        )

        await db["crawl_runs"].update_one(
            {"run_key": run_key},
            {
                "$set": {
                    "status": result["status"],
                    "total": result["total"],
                    "saved": result["saved"],
                    "duplicates": result["duplicates"],
                    "invalid": result["invalid"],
                    "fulltext_queued": result["fulltext_queued"],
                    "fulltext_status": result["fulltext_status"],
                    "per_site": result["per_site"],
                    "errors": result["errors"],
                    "finished_at": finished,
                    "updated_at": finished,
                    "expires_at": run.compute_expires_at(settings.OVERSEAS_NEWS_RUN_RETENTION_DAYS),
                }
            },
        )

        # 6. 自动分类 + 打分（仅当有新文章入库时）
        classify_score_result = {"classified": 0, "scored": 0, "pr_candidates": 0}
        if result["saved"] > 0:
            classify_score_result = await _auto_classify_and_score(
                ctx=ctx,
                db=db,
                run_id=run_id,
                trace_id=trace_id,
                settings=settings,
            )

            # 更新 crawl_runs 记录分类打分结果
            finished_cs = datetime.now(UTC)
            await db["crawl_runs"].update_one(
                {"run_key": run_key},
                {
                    "$set": {
                        "classified": classify_score_result["classified"],
                        "scored": classify_score_result["scored"],
                        "pr_candidates": classify_score_result["pr_candidates"],
                        "classify_errors": classify_score_result.get("errors", {}),
                        "updated_at": finished_cs,
                    }
                },
            )

        # 7. 释放锁
        await db["pipeline_locks"].update_one(
            {"lock_key": lock_key},
            {"$set": {"status": "completed", "updated_at": finished}},
        )

        logger.info(
            "[overseas-schedule] completed: run_id=%s status=%s saved=%d duplicates=%d "
            "classified=%d scored=%d pr_candidates=%d",
            run_id,
            result["status"],
            result["saved"],
            result["duplicates"],
            classify_score_result["classified"],
            classify_score_result["scored"],
            classify_score_result["pr_candidates"],
        )
        result["classified"] = classify_score_result["classified"]
        result["scored"] = classify_score_result["scored"]
        result["pr_candidates"] = classify_score_result["pr_candidates"]
        return result

    except CrawlServiceError as exc:
        finished = datetime.now(UTC)
        await db["crawl_runs"].update_one(
            {"run_key": run_key},
            {
                "$set": {
                    "status": CrawlRunStatus.FAILED.value,
                    "errors": {exc.code: exc.message},
                    "finished_at": finished,
                    "updated_at": finished,
                }
            },
        )
        # 释放锁
        await db["pipeline_locks"].update_one(
            {"lock_key": lock_key},
            {"$set": {"status": "failed", "updated_at": finished}},
        )

        if exc.retryable:
            logger.warning("[overseas-schedule] retryable error: %s", exc.code)
            raise Retry(defer=60) from exc
        else:
            logger.error("[overseas-schedule] non-retryable error: %s", exc.code)
            return {"ok": False, "error": exc.code, "message": exc.message}

    except Exception as exc:
        finished = datetime.now(UTC)
        await db["crawl_runs"].update_one(
            {"run_key": run_key},
            {
                "$set": {
                    "status": CrawlRunStatus.FAILED.value,
                    "errors": {"UNEXPECTED": str(exc)[:500]},
                    "finished_at": finished,
                    "updated_at": finished,
                }
            },
        )
        await db["pipeline_locks"].update_one(
            {"lock_key": lock_key},
            {"$set": {"status": "failed", "updated_at": finished}},
        )
        logger.error("[overseas-schedule] unexpected error: %s", exc)
        raise Retry(defer=120) from exc


def build_cron_jobs(settings: Any = None) -> list[Any]:
    """构建 cron 任务列表，支持配置驱动的时区和开关。

    Args:
        settings: Settings 实例，默认从 get_settings() 获取

    Returns:
        cron 任务列表
    """
    if settings is None:
        settings = get_settings()

    # 统一使用业务时区；原有 hour=19/20（UTC）改为 hour=3/4（本地）
    jobs = [
        cron(cleanup_expired_articles, hour=3, minute=0, microsecond=0),
        cron(decay_user_memories, hour=4, minute=0, microsecond=0),
    ]

    if settings.OVERSEAS_NEWS_SCHEDULE_ENABLED:
        jobs.append(
            cron(
                scheduled_overseas_news_crawl,
                hour=settings.OVERSEAS_NEWS_SCHEDULE_HOUR,
                minute=settings.OVERSEAS_NEWS_SCHEDULE_MINUTE,
                second=0,
                microsecond=0,
                unique=True,
                timeout=settings.OVERSEAS_NEWS_JOB_TIMEOUT_SECONDS,
            )
        )

    return jobs


class WorkerSettings:
    """ARQ worker configuration shared by the worker entry point and tests."""

    functions: ClassVar[list[Any]] = [
        func(execute_pipeline, max_tries=_settings.ARQ_MAX_RETRIES + 1),
        func(resume_pipeline, max_tries=_settings.ARQ_MAX_RETRIES + 1),
        func(fetch_fulltext_batch, max_tries=_settings.ARQ_MAX_RETRIES + 1),
        func(enrich_web_search_articles, max_tries=_settings.ARQ_MAX_RETRIES + 1),
        func(process_memory_event, max_tries=_settings.ARQ_MAX_RETRIES + 1),
        func(compile_memory_summaries, max_tries=_settings.ARQ_MAX_RETRIES + 1),
        func(scheduled_overseas_news_crawl, max_tries=_settings.ARQ_MAX_RETRIES + 1),
    ]
    # 使用业务时区，cron 中的 hour/minute 为本地时间
    from zoneinfo import ZoneInfo

    timezone = ZoneInfo(_settings.OVERSEAS_NEWS_SCHEDULE_TIMEZONE)
    cron_jobs: ClassVar[list[Any]] = build_cron_jobs(_settings)
    redis_settings = redis_settings()
    max_jobs = _settings.ARQ_MAX_JOBS
    job_timeout = _settings.ARQ_JOB_TIMEOUT
    max_tries = _settings.ARQ_MAX_RETRIES + 1
    retry_jobs = True
    health_check_interval = 15
