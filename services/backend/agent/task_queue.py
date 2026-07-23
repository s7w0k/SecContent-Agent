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
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(cleanup_expired_articles, hour=19, minute=0),  # 每天 03:00 UTC+8
    ]
    redis_settings = redis_settings()
    max_jobs = _settings.ARQ_MAX_JOBS
    job_timeout = _settings.ARQ_JOB_TIMEOUT
    max_tries = _settings.ARQ_MAX_RETRIES + 1
    retry_jobs = True
    health_check_interval = 15
