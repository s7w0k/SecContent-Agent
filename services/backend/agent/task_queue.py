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
        from api.pipeline import _execute_pipeline_task

        await _execute_pipeline_task(
            ctx["app"],
            task_id,
            user_id,
            task_type,
            crawl_days=crawl_days,
            article_url_hash=article_url_hash,
            trace_id=trace_id,
            username=username,
            request_id=request_id,
            raise_errors=True,
        )
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
        result = await ctx["pipeline_v2"].resume_from_checkpoint(task_id)
        if result.get("status") != "completed":
            raise RuntimeError(result.get("error") or "checkpoint resume failed")
        return result
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
    cursor = db["user_memory_items"].find({
        "status": {"$in": ["active", "pending_approval", "candidate"]},
        "confirmed_by_user": False,
    })
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
    result_irrelevant = await db["articles"].delete_many({
        "added_at": {"$lt": cutoff_48h},
        "category_v2": "不相关",
    })

    # 14 天前的所有文章
    cutoff_14d = now - timedelta(days=14)
    result_all = await db["articles"].delete_many({
        "added_at": {"$lt": cutoff_14d},
    })

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


class WorkerSettings:
    """ARQ worker configuration shared by the worker entry point and tests."""

    functions: ClassVar[list[Any]] = [
        func(execute_pipeline, max_tries=_settings.ARQ_MAX_RETRIES + 1),
        func(resume_pipeline, max_tries=_settings.ARQ_MAX_RETRIES + 1),
        func(fetch_fulltext_batch, max_tries=_settings.ARQ_MAX_RETRIES + 1),
        func(process_memory_event, max_tries=_settings.ARQ_MAX_RETRIES + 1),
        func(compile_memory_summaries, max_tries=_settings.ARQ_MAX_RETRIES + 1),
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(cleanup_expired_articles, hour=19, minute=0),  # 每天 03:00 UTC+8
        cron(decay_user_memories, hour=20, minute=0),  # 每天 04:00 UTC+8
    ]
    redis_settings = redis_settings()
    max_jobs = _settings.ARQ_MAX_JOBS
    job_timeout = _settings.ARQ_JOB_TIMEOUT
    max_tries = _settings.ARQ_MAX_RETRIES + 1
    retry_jobs = True
    health_check_interval = 15
