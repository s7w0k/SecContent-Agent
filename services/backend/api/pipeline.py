"""
流水线 REST API — 触发与状态查询

端点:
  POST /api/pipeline/run      触发全流程
  POST /api/pipeline/crawl    仅爬取
  POST /api/pipeline/score    仅打分
  POST /api/pipeline/report   仅生成报道
  GET  /api/pipeline/status   查询运行状态
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from agent.checkpointer import create_checkpointer, supports_mongodb_checkpoints
from agent.pipeline_state import PipelineStateManager
from agent.style_profiler import load_style_hints
from api.activity import log_activity
from api.logs import build_log_error, generate_trace_id, log_pipeline
from auth.deps import get_current_user
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from logging_config import get_audit_logger
from models.feedback import PipelineTask
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

router = APIRouter(prefix="/api/pipeline", tags=["Pipeline"])
llm_router = APIRouter(prefix="/api", tags=["LLM Observability"])


# ═══════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════


class PipelineRunRequest(BaseModel):
    crawl_days: int = Field(default=1, ge=1, le=30, description="爬取天数")
    phases: list[str] = Field(
        default=["crawl", "classify", "score", "report"],
        description="要执行的阶段",
    )


class PipelinePhaseRequest(BaseModel):
    crawl_days: int = Field(default=1, ge=1, le=30, description="爬取天数")


class ScoreRequest(BaseModel):
    article_url_hashes: list[str] | None = Field(
        default=None, description="指定文章 hash 列表（留空则对所有已分类文章打分）"
    )


class ReportRequest(BaseModel):
    article_url_hashes: list[str] | None = Field(
        default=None, description="指定文章 hash 列表（留空则对所有高分文章生成报道）"
    )


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _get_manager(request: Request):
    """从 app.state 获取 PipelineManager"""
    manager = getattr(request.app.state, "pipeline_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")
    return manager


def _request_username(request: Request, user_id: str) -> str:
    return getattr(getattr(request, "state", None), "username", None) or user_id


def _request_id(request: Request) -> str:
    return getattr(getattr(request, "state", None), "request_id", None) or ""


async def _get_owned_pipeline_task(db: Any, task_id: str, user_id: str) -> dict[str, Any]:
    """Load one task and distinguish not-found from cross-tenant access."""
    task = await db["pipeline_tasks"].find_one({"task_id": task_id})
    if task is None:
        raise HTTPException(status_code=404, detail="Pipeline task not found")
    if task.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Cannot access another user's task")
    return task


def _checkpoint_node(metadata: dict[str, Any], channel_values: dict[str, Any]) -> str:
    writes = metadata.get("writes")
    if isinstance(writes, dict) and writes:
        return str(next(iter(writes)))
    return str(channel_values.get("current_phase", ""))


async def _read_task_checkpoints(
    db: Any,
    task: dict[str, Any],
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Decode durable LangGraph checkpoints without an in-process graph manager."""
    if not supports_mongodb_checkpoints(db):
        raise HTTPException(status_code=503, detail="Checkpoint storage not available")

    thread_id = task.get("thread_id") or f"thread-{task['task_id']}"
    checkpoint_ns = task.get("checkpoint_ns", "")
    config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        }
    }
    saver = create_checkpointer(db)
    checkpoints: list[dict[str, Any]] = []
    async for item in saver.alist(config, limit=limit):
        configurable = item.config.get("configurable", {})
        parent = (item.parent_config or {}).get("configurable", {})
        checkpoint = item.checkpoint or {}
        metadata = item.metadata or {}
        channel_values = checkpoint.get("channel_values", {})
        checkpoints.append(
            {
                "checkpoint_id": configurable.get("checkpoint_id"),
                "parent_checkpoint_id": parent.get("checkpoint_id"),
                "node": _checkpoint_node(metadata, channel_values),
                "step": metadata.get("step"),
                "created_at": checkpoint.get("ts"),
                "channel_values": jsonable_encoder(channel_values),
            }
        )
    return checkpoints


async def _log_idempotent_skip(
    db: Any,
    request: Request,
    user_id: str,
    phase: str,
    url_hash: str,
    trace_id: str,
    reason: str,
) -> None:
    await log_pipeline(
        db,
        "INFO",
        phase,
        f"{phase} skipped: existing result reused",
        user_id=user_id,
        username=_request_username(request, user_id),
        trace_id=trace_id,
        action="skip",
        detail={"article_url_hash": url_hash, "reason": reason},
    )


PIPELINE_LOCK_TTL_SECONDS = 300
PIPELINE_LOCK_POLL_SECONDS = 3.0


async def acquire_pipeline_lock(
    db: Any,
    lock_key: str,
    user_id: str,
    ttl_seconds: int = PIPELINE_LOCK_TTL_SECONDS,
    *,
    lock_type: str = "crawl",
) -> bool:
    """原子获取短期流水线锁；唯一索引冲突表示锁已被持有。"""
    now = datetime.now(UTC)
    collection = db["pipeline_locks"]
    await collection.delete_one(
        {"lock_key": lock_key, "expires_at": {"$lte": now}},
    )
    try:
        await collection.insert_one(
            {
                "lock_key": lock_key,
                "lock_type": lock_type,
                "status": "running",
                "user_id": user_id,
                "created_at": now,
                "expires_at": now + timedelta(seconds=ttl_seconds),
            },
        )
        return True
    except DuplicateKeyError:
        return False


async def release_pipeline_lock(
    db: Any,
    lock_key: str,
    success: bool = True,
) -> None:
    """成功时保留完成标记供等待者复用；失败时删除锁以允许重试。"""
    collection = db["pipeline_locks"]
    if success:
        await collection.update_one(
            {"lock_key": lock_key},
            {"$set": {"status": "completed", "completed_at": datetime.now(UTC)}},
        )
        return
    await collection.delete_one({"lock_key": lock_key})


async def wait_for_pipeline_lock(
    db: Any,
    lock_key: str,
    timeout: int = PIPELINE_LOCK_TTL_SECONDS,
    *,
    poll_interval: float = PIPELINE_LOCK_POLL_SECONDS,
) -> str:
    """等待已有流水线锁结束，返回 completed、failed 或 timeout。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    collection = db["pipeline_locks"]
    while loop.time() < deadline:
        lock = await collection.find_one({"lock_key": lock_key})
        if not lock:
            return "failed"
        if lock.get("status") == "completed":
            return "completed"
        expires_at = lock.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if isinstance(expires_at, datetime) and expires_at <= datetime.now(UTC):
            await collection.delete_one(
                {"lock_key": lock_key, "expires_at": {"$lte": datetime.now(UTC)}},
            )
            return "failed"
        await asyncio.sleep(poll_interval)
    return "timeout"


def _pipeline_timeout_error() -> HTTPException:
    return HTTPException(
        status_code=408,
        detail={
            "code": "PIPELINE_TIMEOUT",
            "message": "流水线等待超时，请稍后重试",
        },
    )


async def _create_pipeline_task(
    db: Any,
    user_id: str,
    task_type: str,
    article_url_hash: str | None = None,
    *,
    trace_id: str | None = None,
    username: str | None = None,
) -> dict:
    """创建一小时后自动清理的流水线任务文档。"""
    task = PipelineTask(
        user_id=user_id,
        trace_id=trace_id or generate_trace_id(),
        username=username or user_id,
        task_type=task_type,
        article_url_hash=article_url_hash,
        progress={"phase": "pending", "message": "排队中..."},
    )
    document = task.model_dump(exclude={"id"}, mode="python")
    await db["pipeline_tasks"].insert_one(document)
    return document


async def _update_pipeline_task(
    db: Any,
    task_id: str,
    user_id: str,
    *,
    status: str | None = None,
    phase: str | None = None,
    current: int = 0,
    total: int = 0,
    message: str = "",
    result: dict | None = None,
    error: str | None = None,
) -> None:
    """更新指定用户任务状态及进度。"""
    fields: dict[str, Any] = {"updated_at": datetime.now(UTC)}
    if status is not None:
        fields["status"] = status
    if phase is not None:
        fields["progress"] = {
            "phase": phase,
            "current": current,
            "total": total,
            "message": message,
        }
    if result is not None:
        fields["result"] = result
    if error is not None:
        fields["error"] = error[:2000]
    await db["pipeline_tasks"].update_one(
        {"task_id": task_id, "user_id": user_id},
        {"$set": fields},
    )


async def _enqueue_pipeline_task(
    app: Any,
    task_id: str,
    user_id: str,
    task_type: str,
    *,
    crawl_days: int = 1,
    article_url_hash: str | None = None,
    trace_id: str = "",
    username: str = "",
    request_id: str = "",
) -> None:
    """Submit a persisted task to ARQ and fail clearly when Redis is unavailable."""
    pool = getattr(app.state, "arq_pool", None)
    db = getattr(app.state, "db", None)
    if pool is None:
        if db is not None:
            await _update_pipeline_task(
                db,
                task_id,
                user_id,
                status="failed",
                phase="failed",
                message="任务队列不可用",
                error="Task queue not available",
            )
        raise HTTPException(status_code=503, detail="Task queue not available")
    try:
        job = await pool.enqueue_job(
            "execute_pipeline",
            task_id=task_id,
            user_id=user_id,
            task_type=task_type,
            crawl_days=crawl_days,
            article_url_hash=article_url_hash,
            trace_id=trace_id,
            username=username,
            request_id=request_id,
            _job_id=task_id,
        )
    except Exception as exc:
        if db is not None:
            await _update_pipeline_task(
                db,
                task_id,
                user_id,
                status="failed",
                phase="failed",
                message="任务提交失败",
                error=str(exc),
            )
        raise HTTPException(status_code=503, detail="Task queue unavailable") from exc
    if job is None:
        raise HTTPException(status_code=409, detail="Pipeline task is already queued")


async def _execute_pipeline_task(
    app: Any,
    task_id: str,
    user_id: str,
    task_type: str,
    *,
    crawl_days: int = 1,
    article_url_hash: str | None = None,
    trace_id: str | None = None,
    username: str | None = None,
    request_id: str = "",
    raise_errors: bool = False,
) -> None:
    """后台执行流水线任务并持久化阶段进度、结果或错误。"""
    db = app.state.db
    trace_id = trace_id or generate_trace_id()
    username = username or user_id
    task_started = time.perf_counter()
    total_steps = 8 if task_type == "run-v2" and not article_url_hash else 4
    if task_type != "run-v2":
        total_steps = 2
    initial_phase = {
        "classify-v2": "classify_v2",
        "score-v2": "score_v2",
    }.get(task_type, "crawl" if not article_url_hash else "classify")
    initial_message = (
        "正在统计待处理文章..." if task_type in {"classify-v2", "score-v2"} else "正在启动流水线..."
    )
    try:
        await log_pipeline(
            db,
            "INFO",
            "pipeline_task",
            "pipeline task started",
            user_id=user_id,
            username=username,
            trace_id=trace_id,
            action="start",
            detail={"task_id": task_id, "task_type": task_type},
        )
        await _update_pipeline_task(
            db,
            task_id,
            user_id,
            status="running",
            phase=initial_phase,
            current=0,
            total=0 if task_type in {"classify-v2", "score-v2"} else total_steps,
            message=initial_message,
        )

        if task_type == "run-v2" and article_url_hash:
            result = await _run_v2_single_workflow(
                app,
                article_url_hash,
                user_id,
                task_id=task_id,
                trace_id=trace_id,
                username=username,
            )
        elif task_type == "run-v2":
            manager = app.state.pipeline_v2
            result = await manager.run_full(
                crawl_days=crawl_days,
                user_id=user_id,
                trace_id=trace_id,
                username=username,
                request_id=request_id,
                task_id=task_id,
            )
        elif task_type == "crawl":
            result = await _execute_crawl_pipeline(
                app,
                crawl_days,
                user_id,
                trace_id,
                username,
                request_id,
            )
        elif task_type == "classify-v2":
            result = await _run_classify_v2_batch(
                app,
                user_id,
                task_id=task_id,
                trace_id=trace_id,
            )
        elif task_type == "score-v2":
            result = await _run_score_v2_batch(
                app,
                user_id,
                task_id=task_id,
                trace_id=trace_id,
            )
        else:
            raise ValueError(f"Unsupported pipeline task type: {task_type}")

        if result.get("status") in {"failed", "cancelled", "rejected"}:
            raise RuntimeError(result.get("error") or f"Pipeline {result['status']}")
        completed_total = int(result.get("total", total_steps))
        await _update_pipeline_task(
            db,
            task_id,
            user_id,
            status="completed",
            phase="completed",
            current=completed_total,
            total=completed_total,
            message="任务完成",
            result=result,
        )
        await log_activity(
            db,
            user_id,
            "pipeline_run",
            {"task_id": task_id, "article_url_hash": article_url_hash},
            {"crawl_days": crawl_days, "version": "v2" if task_type == "run-v2" else "v1"},
        )
        duration_ms = int((time.perf_counter() - task_started) * 1000)
        await log_pipeline(
            db,
            "INFO",
            "pipeline_task",
            "pipeline task completed",
            user_id=user_id,
            username=username,
            trace_id=trace_id,
            action="complete",
            duration_ms=duration_ms,
            detail={"task_id": task_id, "task_type": task_type, "duration_ms": duration_ms},
        )
    except asyncio.CancelledError:
        await _update_pipeline_task(
            db,
            task_id,
            user_id,
            status="failed",
            phase="failed",
            message="任务已取消",
            error="Pipeline task cancelled",
        )
        exc = asyncio.CancelledError("Pipeline task cancelled")
        await log_pipeline(
            db,
            "ERROR",
            "pipeline_task",
            "pipeline task cancelled",
            user_id=user_id,
            username=username,
            trace_id=trace_id,
            action="error",
            duration_ms=int((time.perf_counter() - task_started) * 1000),
            error=build_log_error(exc),
            detail={"task_id": task_id, "task_type": task_type},
        )
        raise
    except Exception as exc:
        logging.getLogger("backend.api.pipeline").exception(
            "Pipeline task failed: task_id=%s user_id=%s",
            task_id,
            user_id,
        )
        await _update_pipeline_task(
            db,
            task_id,
            user_id,
            status="failed",
            phase="failed",
            message="任务执行失败",
            error=str(exc),
        )
        await log_pipeline(
            db,
            "ERROR",
            "pipeline_task",
            "pipeline task failed",
            user_id=user_id,
            username=username,
            trace_id=trace_id,
            action="error",
            duration_ms=int((time.perf_counter() - task_started) * 1000),
            error=build_log_error(exc),
            detail={"task_id": task_id, "task_type": task_type},
        )
        if raise_errors:
            raise


# ═══════════════════════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════════════════════


@router.post("/run", summary="触发完整流水线 (V1)")
async def pipeline_run(
    body: PipelineRunRequest, request: Request, user_id: str = Depends(get_current_user)
):
    """执行全流程：crawl → classify → score → report"""
    manager = _get_manager(request)
    trace_id = generate_trace_id()
    username = _request_username(request, user_id)
    result = await manager.run_full(
        crawl_days=body.crawl_days,
        user_id=user_id,
        trace_id=trace_id,
        username=username,
        request_id=_request_id(request),
    )
    await log_activity(
        getattr(request.app.state, "db", None),
        user_id,
        "pipeline_run",
        {"pipeline_id": result.get("pipeline_id") if isinstance(result, dict) else None},
        {"crawl_days": body.crawl_days, "version": "v1"},
    )
    if isinstance(result, dict):
        result["trace_id"] = trace_id
    return result


@router.post("/run-v2", summary="触发 V2 智能 PR 流水线")
async def pipeline_run_v2(
    body: PipelineRunRequest, request: Request, user_id: str = Depends(get_current_user)
):
    """创建 V2 全流程后台任务并立即返回 task_id。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        raise HTTPException(status_code=503, detail="Task queue not available")

    trace_id = generate_trace_id()
    username = _request_username(request, user_id)
    task = await _create_pipeline_task(db, user_id, "run-v2", trace_id=trace_id, username=username)
    await _enqueue_pipeline_task(
        request.app,
        task["task_id"],
        user_id,
        "run-v2",
        crawl_days=body.crawl_days,
        trace_id=trace_id,
        username=username,
        request_id=_request_id(request),
    )
    get_audit_logger().log(
        user_id=user_id,
        action="pipeline_trigger",
        detail={"task_type": "run-v2"},
    )
    return {
        "ok": True,
        "data": {
            "task_id": task["task_id"],
            "trace_id": trace_id,
            "message": "任务已创建，请轮询状态",
        },
    }


async def _execute_crawl_pipeline(
    app: Any,
    crawl_days: int,
    user_id: str,
    trace_id: str = "",
    username: str = "",
    request_id: str = "",
) -> dict:
    """执行带共享锁的爬取和分类，供后台任务复用。"""
    manager = app.state.pipeline_manager
    db = app.state.db
    lock_key = f"crawl-{datetime.now(UTC):%Y-%m-%d}-days-{crawl_days}"
    acquired = await acquire_pipeline_lock(db, lock_key, user_id)
    retry_count = 0
    while not acquired:
        status = await wait_for_pipeline_lock(db, lock_key)
        if status == "completed":
            await log_pipeline(
                db,
                "INFO",
                "crawl",
                "crawl result reused from shared execution",
                user_id=user_id,
                username=username,
                trace_id=trace_id,
                action="skip",
                detail={"lock_key": lock_key, "reason": "shared_result_reused"},
            )
            return {
                "ok": True,
                "skipped": True,
                "data": {
                    "message": "爬取已由其他用户完成，复用结果",
                    "skipped": True,
                },
            }
        if status == "timeout" or retry_count >= 1:
            raise _pipeline_timeout_error()
        retry_count += 1
        acquired = await acquire_pipeline_lock(db, lock_key, user_id)

    try:
        result = await manager.run_phase(
            "classify",
            crawl_days=crawl_days,
            user_id=user_id,
            trace_id=trace_id,
            username=username,
            request_id=request_id,
        )
    except Exception:
        await release_pipeline_lock(db, lock_key, success=False)
        raise

    await release_pipeline_lock(db, lock_key, success=True)
    return result


@router.get("/status-v2", summary="查询 V2 流水线状态")
async def pipeline_status_v2(
    request: Request,
    task_id: str | None = Query(default=None),
    user_id: str = Depends(get_current_user),
):
    """从 MongoDB 返回当前用户指定或最近的 V2 流水线状态。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    if task_id:
        task = await _get_owned_pipeline_task(db, task_id, user_id)
    else:
        task = await db["pipeline_tasks"].find_one(
            {"user_id": user_id, "task_type": "run-v2"},
            sort=[("created_at", -1)],
        )
    if task is None:
        return {
            "status": "idle",
            "current_phase": "",
            "state": {},
            "errors": [],
        }

    state = task.get("state") or {}
    return {
        "task_id": task["task_id"],
        "status": task.get("status", "pending"),
        "current_phase": state.get("current_phase", task.get("progress", {}).get("phase", "")),
        "progress": task.get("progress", {}),
        "last_node": task.get("last_node", ""),
        "retry_count": task.get("retry_count", 0),
        "state": state,
        "errors": state.get("errors", []) or ([task["error"]] if task.get("error") else []),
    }


@router.post("/crawl", summary="爬取+分类")
async def pipeline_crawl(
    body: PipelinePhaseRequest, request: Request, user_id: str = Depends(get_current_user)
):
    """创建爬取后台任务并立即返回 task_id。"""
    _get_manager(request)
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    trace_id = generate_trace_id()
    username = _request_username(request, user_id)
    task = await _create_pipeline_task(db, user_id, "crawl", trace_id=trace_id, username=username)
    await _enqueue_pipeline_task(
        request.app,
        task["task_id"],
        user_id,
        "crawl",
        crawl_days=body.crawl_days,
        trace_id=trace_id,
        username=username,
        request_id=_request_id(request),
    )
    get_audit_logger().log(
        user_id=user_id,
        action="crawl_trigger",
    )
    return {
        "ok": True,
        "data": {
            "task_id": task["task_id"],
            "trace_id": trace_id,
            "message": "任务已创建，请轮询状态",
        },
    }


@router.post("/crawl-overseas", summary="仅爬取海外安全新闻")
async def crawl_overseas_only(
    request: Request, days: int = 1, _user_id: str = Depends(get_current_user)
):
    """仅调 mcp-crawl 爬取海外新闻 → 入库"""
    import hashlib
    from datetime import datetime, timedelta, timezone

    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        raise HTTPException(status_code=503, detail="Task queue not available")

    tz = timezone(timedelta(hours=8))
    tools = getattr(request.app.state, "pipeline_manager", None)
    if tools is None:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")

    try:
        trace_id = generate_trace_id()
        request_id = _request_id(request)
        result = await tools.tools["crawl_overseas_news"].ainvoke(
            {
                "payload": {
                    "days": days,
                    "_request_context": {
                        "request_id": request_id,
                        "trace_id": trace_id,
                        "initiator_user_id": _user_id,
                    },
                }
            }
        )
        data: dict[str, Any] = {}
        articles = []
        if result.get("ok") and result.get("data"):
            data = result["data"]
            articles = data.get("articles", []) if isinstance(data, dict) else data

        saved = 0
        new_urls: list[dict] = []
        for art in articles:
            url = art.get("url", "")
            if not url:
                continue
            url_hash = hashlib.md5(url.encode()).hexdigest()
            existing = await db["articles"].find_one({"url_hash": url_hash})
            if existing:
                continue
            await db["articles"].insert_one(
                {
                    "url_hash": url_hash,
                    "title": art.get("title", ""),
                    "url": url,
                    "source": art.get("source", ""),
                    "source_type": "overseas_news",
                    "published_at": art.get("published_at", ""),
                    "added_at": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    "summary": art.get("summary", ""),
                    "content_md": "",
                    "pipeline_status": "crawled",
                }
            )
            new_urls.append({"url_hash": url_hash, "url": url})
            saved += 1

        # 全文抓取交由 ARQ Worker，API 进程不持有后台协程。
        if new_urls:
            await arq_pool.enqueue_job(
                "fetch_fulltext_batch",
                new_urls,
                trace_id=trace_id,
                user_id=_user_id,
                request_id=request_id,
            )

        errors = data.get("errors", {}) if isinstance(data, dict) else {}
        per_site = data.get("per_site", {}) if isinstance(data, dict) else {}
        return {
            "ok": True,
            "total": len(articles),
            "saved": saved,
            "errors": errors,
            "per_site": per_site,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/crawl-wewe", summary="仅爬取公众号文章")
async def crawl_wewe_only(request: Request, _user_id: str = Depends(get_current_user)):
    """仅直连 WeWe RSS Atom feed 爬取公众号文章 → 入库"""
    import hashlib
    import xml.etree.ElementTree as ET
    from datetime import datetime, timedelta, timezone

    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    tz = timezone(timedelta(hours=8))
    log = logging.getLogger("backend.api.pipeline")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("http://49.232.145.182:4001/feeds/all.atom")
            xml_text = resp.text

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_text)
        entries = root.findall("atom:entry", ns)
        saved = 0
        for entry in entries:
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            author_el = entry.find("atom:author/atom:name", ns)
            updated_el = entry.find("atom:updated", ns)
            title = title_el.text if title_el is not None else ""
            url = link_el.get("href", "") if link_el is not None else ""
            source = author_el.text if author_el is not None else "微信公众号"
            pub = (
                updated_el.text[:10].replace("-", "年", 1).replace("-", "月") + "日"
                if updated_el is not None and updated_el.text
                else ""
            )
            if not url:
                continue
            url_hash = hashlib.md5(url.encode()).hexdigest()
            existing = await db["articles"].find_one({"url_hash": url_hash})
            if existing:
                continue
            await db["articles"].insert_one(
                {
                    "url_hash": url_hash,
                    "title": title,
                    "url": url,
                    "source": source,
                    "source_type": "wechat_mp",
                    "published_at": pub,
                    "added_at": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    "summary": "",
                    "content_md": "",
                    "pipeline_status": "crawled",
                }
            )
            saved += 1
        log.info(f"[crawl-wewe] Saved {saved}/{len(entries)}")
        return {"ok": True, "total": len(entries), "saved": saved}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/score", summary="仅执行打分阶段")
async def pipeline_score(
    body: ScoreRequest, request: Request, user_id: str = Depends(get_current_user)
):
    """对已分类文章重新打分"""
    manager = _get_manager(request)
    # 仅执行 score 阶段（跳过 crawl 和 classify）
    trace_id = generate_trace_id()
    result = await manager.run_phase(
        "score",
        crawl_days=0,
        user_id=user_id,
        trace_id=trace_id,
        username=_request_username(request, user_id),
        request_id=_request_id(request),
    )
    result["trace_id"] = trace_id
    return result


@router.post("/report", summary="仅生成报道")
async def pipeline_report(
    body: ReportRequest, request: Request, user_id: str = Depends(get_current_user)
):
    """对高分文章生成 PR 报道"""
    manager = _get_manager(request)
    trace_id = generate_trace_id()
    result = await manager.run_phase(
        "report",
        crawl_days=0,
        user_id=user_id,
        trace_id=trace_id,
        username=_request_username(request, user_id),
        request_id=_request_id(request),
    )
    result["trace_id"] = trace_id
    return result


@router.post("/crawl-api", summary="API 抓取公众号文章")
async def crawl_via_api(request: Request, days: int = 1, _user_id: str = Depends(get_current_user)):
    """通过 Just One API 抓取指定公众号文章 → 逐篇抓取全文 → 入库。"""
    import hashlib
    import os
    from datetime import datetime, timedelta, timezone

    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    api_url = os.getenv("JUST_ONE_API_URL", "https://api.justoneapi.com")
    api_token = os.getenv("JUST_ONE_API_TOKEN", "swgbMkTrhfvwP6Rv")
    # 从 MongoDB 读取配置的公众号列表
    cursor = db["crawl_accounts"].find().sort("name", 1)
    configs = await cursor.to_list(length=100)
    accounts = (
        [c["name"] for c in configs]
        if configs
        else os.getenv("JUST_ONE_ACCOUNTS", "安恒信息,奇安信集团,绿盟科技").split(",")
    )

    log = logging.getLogger("backend.api.pipeline")
    tz = timezone(timedelta(hours=8))
    all_articles = []

    try:
        # 1. 逐个公众号调 API（需要传 name 参数）
        async with httpx.AsyncClient(timeout=30) as client:
            for account in accounts:
                account = account.strip()
                if not account:
                    continue
                try:
                    resp = await client.get(
                        f"{api_url}/api/weixin/get-account-today-articles/v1"
                        f"?token={api_token}&name={account}"
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    arts = data.get("data", []) or data.get("articles", [])
                    if isinstance(arts, list):
                        for a in arts:
                            a["_source_account"] = account
                        all_articles.extend(arts)
                    log.info(
                        f"[crawl-api] {account}: {len(arts) if isinstance(arts, list) else 0} articles"
                    )
                except Exception as e:
                    log.warning(f"[crawl-api] {account} failed: {e}")

        log.info(f"[crawl-api] Total articles from API: {len(all_articles)}")

        # 2. 逐篇抓取全文 + 入库
        saved, skipped = 0, 0
        async with httpx.AsyncClient(timeout=60) as client:
            for art in all_articles:
                url = art.get("url", "") or art.get("link", "")
                title = art.get("title", "")
                if not url:
                    continue

                url_hash = hashlib.md5(url.encode()).hexdigest()
                existing = await db["articles"].find_one({"url_hash": url_hash})
                if existing:
                    skipped += 1
                    continue

                # 抓取全文 (mcp-wewe)
                content = ""
                try:
                    fr = await client.post("http://mcp-wewe:8100/fetch-article", json={"link": url})
                    fd = fr.json()
                    content = fd.get("text", "") or ""
                except Exception:
                    pass

                source = art.get(
                    "_source_account", art.get("author_name", art.get("author", "微信公众号"))
                )
                pub = art.get("publish_time", art.get("pub_time", art.get("created_at", "")))

                await db["articles"].insert_one(
                    {
                        "url_hash": url_hash,
                        "title": title,
                        "url": url,
                        "source": source,
                        "source_type": "wechat_mp",
                        "published_at": pub,
                        "added_at": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                        "summary": art.get(
                            "digest", art.get("summary", art.get("description", ""))
                        ),
                        "content_md": content[:50000],
                        "pipeline_status": "crawled",
                    }
                )
                saved += 1

        log.info(f"[crawl-api] Saved {saved}, skipped {skipped}")
        return {"ok": True, "total": len(all_articles), "saved": saved, "skipped": skipped}

    except Exception as e:
        log.error(f"[crawl-api] Failed: {e}")
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/status", summary="查询流水线状态")
async def pipeline_status(request: Request, _user_id: str = Depends(get_current_user)):
    """返回当前流水线的运行状态和进度"""
    manager = _get_manager(request)
    return manager.get_status()


@router.post("/import-wewe", summary="导入 WeWe RSS 全部文章")
async def import_wewe_articles(request: Request, _user_id: str = Depends(get_current_user)):
    """从 WeWe RSS 获取全部文章并入库（含公众号来源和中文日期）。"""
    import hashlib
    from datetime import datetime, timedelta, timezone

    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    log = logging.getLogger("backend.api.pipeline")
    log.info("[import-wewe] Starting WeWe article import...")

    try:
        # 1. 获取 Atom feed（含 <author><name> 公众号名称）
        import xml.etree.ElementTree as ET

        atom_url = "http://49.232.145.182:4001/feeds/all.atom"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(atom_url)
            xml_text = resp.text

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_text)
        entries = root.findall("atom:entry", ns)
        log.info(f"[import-wewe] Atom feed has {len(entries)} articles")

        # 2. 入库
        saved = 0
        tz = timezone(timedelta(hours=8))
        for entry in entries:
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            author_el = entry.find("atom:author/atom:name", ns)
            updated_el = entry.find("atom:updated", ns)

            title = title_el.text if title_el is not None else ""
            url = link_el.get("href", "") if link_el is not None else ""
            source_name = author_el.text if author_el is not None else "微信公众号"
            pub_date_raw = updated_el.text if updated_el is not None else ""

            if not url:
                continue

            # 日期格式转换: "2026-06-29T01:02:20.000Z" → "2026年6月29日"
            pub_date = (
                pub_date_raw[:10].replace("-", "年", 1).replace("-", "月") + "日"
                if pub_date_raw
                else ""
            )

            url_hash = hashlib.md5(url.encode()).hexdigest()
            existing = await db["articles"].find_one({"url_hash": url_hash})
            if existing:
                continue

            doc = {
                "url_hash": url_hash,
                "title": title,
                "url": url,
                "source": source_name,
                "source_type": "wechat_mp",
                "published_at": pub_date,
                "added_at": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "summary": "",
                "summary_cn": "",
                "content_md": "",
                "is_ai_security": False,
                "is_agent_security": False,
                "category": "",
                "ai_relevance_score": 0,
                "reportability_score": 0,
                "score_reason": "",
                "has_report": False,
                "report_id": None,
                "pipeline_status": "crawled",
            }
            await db["articles"].insert_one(doc)
            saved += 1

        log.info(f"[import-wewe] Saved {saved} new ({len(entries) - saved} dupes)")
        return {"ok": True, "total": len(entries), "saved": saved}

    except Exception as e:
        log.error(f"[import-wewe] Failed: {e}")
        raise HTTPException(status_code=502, detail=str(e)) from e


# ═══════════════════════════════════════════════════════════════
# V2 6分类端点
# ═══════════════════════════════════════════════════════════════


class ClassifyV2Request(BaseModel):
    url_hashes: list[str] | None = Field(
        default=None,
        description="指定文章 hash 列表（留空则对所有 crawled 或 classified 文章分类）",
    )
    force: bool = Field(default=False, description="强制重新分类（忽略已有 category_v2）")


def _classification_payload(article: dict, *, skipped: bool) -> dict:
    return {
        "ok": True,
        "category": article.get("category_v2", ""),
        "confidence": article.get("category_v2_confidence", 0),
        "is_pr_eligible": article.get("is_pr_eligible", False),
        "skipped": skipped,
    }


V2_BATCH_CONCURRENCY = 5


async def _run_classify_v2_batch(
    app: Any,
    user_id: str,
    *,
    task_id: str,
    trace_id: str = "",
) -> dict[str, Any]:
    """并发执行 V2 分类，并在每篇完成后持久化真实文章进度。"""
    db = app.state.db
    classifier = app.state.classifier_v2
    log = logging.getLogger("backend.api.pipeline")
    query = {
        "pipeline_status": {"$in": ["crawled", "classified"]},
        "category_v2": {"$in": ["", None]},
    }
    articles = await db["articles"].find(query).to_list(length=500)
    total = len(articles)
    await _update_pipeline_task(
        db,
        task_id,
        user_id,
        status="running",
        phase="classify_v2",
        current=0,
        total=total,
        message=f"共 {total} 篇待分类，准备开始...",
    )
    if total == 0:
        return {"ok": True, "total": 0, "classified": 0, "summary": {}}

    semaphore = asyncio.Semaphore(V2_BATCH_CONCURRENCY)

    async def classify_one(article: dict[str, Any]) -> tuple[Any, bool]:
        async with semaphore:
            result = await classifier.classify_single(
                article,
                user_id=user_id,
                trace_id=trace_id,
                task_id=task_id,
            )
        try:
            await db["articles"].update_one(
                {"_id": article["_id"]},
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
            return result, True
        except Exception as exc:
            log.warning("[classify-v2] DB update failed: %s", exc)
            return result, False

    classified = 0
    summary: dict[str, int] = {}
    futures = [asyncio.create_task(classify_one(article)) for article in articles]
    for completed, future in enumerate(asyncio.as_completed(futures), start=1):
        result, updated = await future
        classified += int(updated)
        summary[result.category] = summary.get(result.category, 0) + 1
        remaining = total - completed
        await _update_pipeline_task(
            db,
            task_id,
            user_id,
            status="running",
            phase="classify_v2",
            current=completed,
            total=total,
            message=f"已分类 {completed} 篇，剩余 {remaining} 篇",
        )

    log.info("[classify-v2] Done: %d/%d updated", classified, total)
    return {
        "ok": True,
        "total": total,
        "classified": classified,
        "summary": summary,
    }


async def _run_score_v2_batch(
    app: Any,
    user_id: str,
    *,
    task_id: str,
    trace_id: str = "",
) -> dict[str, Any]:
    """并发执行 V2 打分，并在每篇完成后持久化真实文章进度。"""
    db = app.state.db
    scorer = app.state.scorer_v2
    log = logging.getLogger("backend.api.pipeline")
    articles = (
        await db["articles"]
        .find({"is_pr_eligible": True, "pr_total_score": None})
        .to_list(length=500)
    )
    total = len(articles)
    await _update_pipeline_task(
        db,
        task_id,
        user_id,
        status="running",
        phase="score_v2",
        current=0,
        total=total,
        message=f"共 {total} 篇待打分，准备开始...",
    )
    if total == 0:
        return {"ok": True, "total": 0, "scored": 0, "candidates": 0}

    semaphore = asyncio.Semaphore(V2_BATCH_CONCURRENCY)

    async def score_one(article: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        async with semaphore:
            result = await scorer.score_single(
                article,
                user_id=user_id,
                trace_id=trace_id,
                task_id=task_id,
            )
        if result.get("_fallback", True):
            return result, False
        await db["articles"].update_one(
            {"_id": article["_id"]},
            {
                "$set": {
                    "product_relevance": result["product_relevance"],
                    "event_impact": result["event_impact"],
                    "pr_total_score": result["pr_total_score"],
                    "score_reason": result.get("score_reason", ""),
                }
            },
        )
        return result, True

    scored = 0
    candidates = 0
    futures = [asyncio.create_task(score_one(article)) for article in articles]
    for completed, future in enumerate(asyncio.as_completed(futures), start=1):
        result, updated = await future
        scored += int(updated)
        candidates += int(updated and result.get("is_pr_candidate", False))
        remaining = total - completed
        await _update_pipeline_task(
            db,
            task_id,
            user_id,
            status="running",
            phase="score_v2",
            current=completed,
            total=total,
            message=f"已打分 {completed} 篇，剩余 {remaining} 篇",
        )

    log.info("[score-v2] Scored: %d/%d, %d PR candidates", scored, total, candidates)
    return {
        "ok": True,
        "total": total,
        "scored": scored,
        "candidates": candidates,
    }


async def _create_v2_batch_task(
    request: Request,
    user_id: str,
    task_type: str,
    total: int,
) -> dict[str, Any]:
    db = request.app.state.db
    trace_id = generate_trace_id()
    username = _request_username(request, user_id)
    task = await _create_pipeline_task(
        db,
        user_id,
        task_type,
        trace_id=trace_id,
        username=username,
    )
    action = "分类" if task_type == "classify-v2" else "打分"
    await _update_pipeline_task(
        db,
        task["task_id"],
        user_id,
        status="pending",
        phase=task_type.replace("-", "_"),
        current=0,
        total=total,
        message=f"共 {total} 篇待{action}，等待执行...",
    )
    await _enqueue_pipeline_task(
        request.app,
        task["task_id"],
        user_id,
        task_type,
        trace_id=trace_id,
        username=username,
        request_id=_request_id(request),
    )
    return {
        "ok": True,
        "data": {
            "task_id": task["task_id"],
            "trace_id": trace_id,
            "total": total,
            "message": f"任务已创建，共 {total} 篇待{action}",
        },
    }


@router.post("/classify-v2/tasks", summary="创建 V2 批量分类后台任务")
async def create_classify_v2_task(request: Request, user_id: str = Depends(get_current_user)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    if getattr(request.app.state, "classifier_v2", None) is None:
        raise HTTPException(status_code=503, detail="ClassifierV2 not initialized")
    total = min(
        await db["articles"].count_documents(
            {
                "pipeline_status": {"$in": ["crawled", "classified"]},
                "category_v2": {"$in": ["", None]},
            }
        ),
        500,
    )
    return await _create_v2_batch_task(request, user_id, "classify-v2", total)


@router.post("/score-v2/tasks", summary="创建 V2 批量打分后台任务")
async def create_score_v2_task(request: Request, user_id: str = Depends(get_current_user)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    if getattr(request.app.state, "scorer_v2", None) is None:
        raise HTTPException(status_code=503, detail="ScoringAgentV2 not initialized")
    total = min(
        await db["articles"].count_documents({"is_pr_eligible": True, "pr_total_score": None}),
        500,
    )
    return await _create_v2_batch_task(request, user_id, "score-v2", total)


@router.post("/classify-v2/{url_hash}", summary="单篇文章 V2 6分类")
async def classify_v2_single(
    url_hash: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """幂等分类单篇文章，并用文章级短期锁避免并发重复调用 LLM。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    classifier = getattr(request.app.state, "classifier_v2", None)
    if classifier is None:
        raise HTTPException(status_code=503, detail="ClassifierV2 not initialized")
    trace_id = generate_trace_id()

    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    if article.get("category_v2"):
        await _log_idempotent_skip(
            db, request, user_id, "classify_v2", url_hash, trace_id, "already_classified"
        )
        return _classification_payload(article, skipped=True)

    lock_key = f"classify-v2:{url_hash}"
    acquired = await acquire_pipeline_lock(
        db,
        lock_key,
        user_id,
        lock_type="classify",
    )
    if not acquired:
        status = await wait_for_pipeline_lock(db, lock_key)
        if status == "timeout":
            raise _pipeline_timeout_error()
        article = await db["articles"].find_one({"url_hash": url_hash})
        if article and article.get("category_v2"):
            await _log_idempotent_skip(
                db, request, user_id, "classify_v2", url_hash, trace_id, "concurrent_result"
            )
            return _classification_payload(article, skipped=True)
        acquired = await acquire_pipeline_lock(
            db,
            lock_key,
            user_id,
            lock_type="classify",
        )
        if not acquired:
            raise _pipeline_timeout_error()

    try:
        article = await db["articles"].find_one({"url_hash": url_hash})
        if article and article.get("category_v2"):
            await release_pipeline_lock(db, lock_key, success=True)
            await _log_idempotent_skip(
                db, request, user_id, "classify_v2", url_hash, trace_id, "locked_recheck"
            )
            return _classification_payload(article, skipped=True)

        result = await classifier.classify_single(
            dict(article),
            user_id=user_id,
            trace_id=trace_id,
        )
        classified = {
            "category_v2": result.category,
            "category_v2_confidence": result.confidence,
            "category_v2_reason": result.reason,
            "category_v2_fallback": result.is_fallback,
            "is_pr_eligible": result.is_pr_eligible,
        }
        await db["articles"].update_one(
            {
                "url_hash": url_hash,
                "$or": [
                    {"category_v2": {"$exists": False}},
                    {"category_v2": {"$in": ["", None]}},
                ],
            },
            {"$set": classified},
        )
    except Exception as exc:
        await release_pipeline_lock(db, lock_key, success=False)
        logging.getLogger("backend.api.pipeline").error(
            "[classify-v2-single] Failed: %s",
            exc,
        )
        await log_pipeline(
            db,
            "ERROR",
            "classify_v2",
            "single article classification failed",
            user_id=user_id,
            username=_request_username(request, user_id),
            trace_id=trace_id,
            action="error",
            error=build_log_error(exc),
            detail={"article_url_hash": url_hash},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await release_pipeline_lock(db, lock_key, success=True)
    return _classification_payload(classified, skipped=False)


@router.post("/classify-v2", summary="V2 6分类")
async def classify_v2(
    body: ClassifyV2Request, request: Request, user_id: str = Depends(get_current_user)
):
    """对文章执行6分类（爆点事件/法律法规/AI进展/竞品/行业/学术）。

    读取 pipeline_status 为 crawled 或 classified 的文章，
    调用 LLM 进行6类别归类，更新 category_v2 字段。
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    classifier = getattr(request.app.state, "classifier_v2", None)
    if classifier is None:
        raise HTTPException(status_code=503, detail="ClassifierV2 not initialized")

    log = logging.getLogger("backend.api.pipeline")

    try:
        # 查询待分类文章
        query: dict = {}
        if body.url_hashes:
            query["url_hash"] = {"$in": body.url_hashes}
        else:
            query["pipeline_status"] = {"$in": ["crawled", "classified"]}
            if not body.force:
                query["category_v2"] = {"$in": ["", None]}

        cursor = db["articles"].find(query)
        articles = await cursor.to_list(length=500)
        log.info(f"[classify-v2] Found {len(articles)} articles to classify")

        if not articles:
            return {"ok": True, "total": 0, "classified": 0, "results": []}

        # 批量分类
        results = await classifier.classify_batch(articles, user_id=user_id)

        # 更新数据库
        updated = 0
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
            except Exception as e:
                log.warning(f"[classify-v2] DB update failed: {e}")

        summary = {}
        for r in results:
            cat = r.category
            summary[cat] = summary.get(cat, 0) + 1

        log.info(
            f"[classify-v2] Done: {updated}/{len(articles)} updated, "
            f"{sum(1 for r in results if r.is_pr_eligible)} PR-eligible"
        )
        get_audit_logger().log(
            user_id=user_id,
            action="classify_v2",
        )
        return {
            "ok": True,
            "total": len(articles),
            "classified": updated,
            "summary": summary,
            "results": [r.to_dict() for r in results],
        }

    except Exception as e:
        log.error(f"[classify-v2] Failed: {e}")
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/run-v2/{url_hash}", summary="单文章 V2 智能 PR 流水线")
async def run_v2_single(url_hash: str, request: Request, user_id: str = Depends(get_current_user)):
    """创建单篇文章 V2 后台任务并立即返回 task_id。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    if getattr(request.app.state, "classifier_v2", None) is None:
        raise HTTPException(status_code=503, detail="ClassifierV2 not initialized")
    if await db["articles"].find_one({"url_hash": url_hash}) is None:
        raise HTTPException(status_code=404, detail="Article not found")

    trace_id = generate_trace_id()
    username = _request_username(request, user_id)
    task = await _create_pipeline_task(
        db,
        user_id,
        "run-v2",
        url_hash,
        trace_id=trace_id,
        username=username,
    )
    await _enqueue_pipeline_task(
        request.app,
        task["task_id"],
        user_id,
        "run-v2",
        article_url_hash=url_hash,
        trace_id=trace_id,
        username=username,
        request_id=_request_id(request),
    )
    get_audit_logger().log(
        user_id=user_id,
        action="pipeline_trigger_single",
        resource=url_hash,
    )
    return {
        "ok": True,
        "data": {
            "task_id": task["task_id"],
            "trace_id": trace_id,
            "message": "任务已创建，请轮询状态",
        },
    }


async def _run_v2_single_workflow(
    app: Any,
    url_hash: str,
    user_id: str,
    *,
    task_id: str | None = None,
    trace_id: str | None = None,
    username: str | None = None,
) -> dict:
    """执行单篇分类、评分和个性化草稿生成，由后台任务调用。"""
    db = app.state.db
    classifier = app.state.classifier_v2
    log = logging.getLogger("backend.api.pipeline")
    trace_id = trace_id or generate_trace_id()
    username = username or user_id

    async def update_progress(phase: str, current: int, message: str) -> None:
        if task_id:
            await _update_pipeline_task(
                db,
                task_id,
                user_id,
                status="running",
                phase=phase,
                current=current,
                total=4,
                message=message,
            )

    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    result = {"url_hash": url_hash, "title": article.get("title", ""), "steps": []}

    classify_started = time.perf_counter()
    await update_progress("classify", 0, "正在分类文章...")
    await log_pipeline(
        db,
        "INFO",
        "classify_v2",
        "single article classification started",
        user_id=user_id,
        username=username,
        trace_id=trace_id,
        action="start",
        detail={"article_url_hash": url_hash, "task_id": task_id},
    )
    if article.get("category_v2"):
        category = article["category_v2"]
        confidence = article.get("category_v2_confidence", 0)
        is_pr_eligible = article.get("is_pr_eligible", False)
        classification_skipped = True
    else:
        classify_result = await classifier.classify_single(
            dict(article),
            user_id=user_id,
            trace_id=trace_id,
            task_id=task_id,
        )
        category = classify_result.category
        confidence = classify_result.confidence
        is_pr_eligible = classify_result.is_pr_eligible
        classification_skipped = False
        await db["articles"].update_one(
            {"url_hash": url_hash},
            {
                "$set": {
                    "category_v2": category,
                    "category_v2_confidence": confidence,
                    "category_v2_reason": classify_result.reason,
                    "category_v2_fallback": classify_result.is_fallback,
                    "is_pr_eligible": is_pr_eligible,
                }
            },
        )
    result["steps"].append(
        {
            "phase": "classify_v2",
            "category": category,
            "confidence": confidence,
            "is_pr_eligible": is_pr_eligible,
            "skipped": classification_skipped,
        }
    )
    log.info("[run-v2-single] Classify: %s (PR=%s)", category, is_pr_eligible)
    classify_duration = int((time.perf_counter() - classify_started) * 1000)
    await log_pipeline(
        db,
        "INFO",
        "classify_v2",
        "single article classification reused"
        if classification_skipped
        else "single article classified",
        user_id=user_id,
        username=username,
        trace_id=trace_id,
        action="skip" if classification_skipped else "complete",
        duration_ms=classify_duration,
        detail={
            "article_url_hash": url_hash,
            "category": category,
            "confidence": confidence,
            "is_pr_eligible": is_pr_eligible,
        },
    )

    if is_pr_eligible:
        score_started = time.perf_counter()
        await update_progress("score", 1, "正在评估文章...")
        await log_pipeline(
            db,
            "INFO",
            "score_v2",
            "single article scoring started",
            user_id=user_id,
            username=username,
            trace_id=trace_id,
            action="start",
            detail={"article_url_hash": url_hash},
        )
        scorer = getattr(app.state, "scorer_v2", None)
        if scorer:
            if article.get("pr_total_score") is not None:
                scores = {
                    "product_relevance": article.get("product_relevance", 0),
                    "event_impact": article.get("event_impact", 0),
                    "pr_total_score": article["pr_total_score"],
                    "score_reason": article.get("score_reason", ""),
                    "is_pr_candidate": article.get(
                        "is_pr_candidate",
                        article["pr_total_score"] >= 80,
                    ),
                }
                score_skipped = True
            else:
                scores = await scorer.score_single(
                    dict(article),
                    user_id=user_id,
                    trace_id=trace_id,
                    task_id=task_id,
                )
                score_skipped = False
                await db["articles"].update_one(
                    {"url_hash": url_hash},
                    {
                        "$set": {
                            "product_relevance": scores["product_relevance"],
                            "event_impact": scores["event_impact"],
                            "pr_total_score": scores["pr_total_score"],
                            "score_reason": scores.get("score_reason", ""),
                        }
                    },
                )
            result["steps"].append(
                {
                    "phase": "score_v2",
                    "product_relevance": scores["product_relevance"],
                    "event_impact": scores["event_impact"],
                    "pr_total_score": scores["pr_total_score"],
                    "is_pr_candidate": scores.get("is_pr_candidate", False),
                    "skipped": score_skipped,
                }
            )
            log.info(
                "[run-v2-single] Score: %s (candidate=%s)",
                scores["pr_total_score"],
                scores.get("is_pr_candidate"),
            )
            score_duration = int((time.perf_counter() - score_started) * 1000)
            await log_pipeline(
                db,
                "INFO",
                "score_v2",
                "single article score reused" if score_skipped else "single article scored",
                user_id=user_id,
                username=username,
                trace_id=trace_id,
                action="skip" if score_skipped else "complete",
                duration_ms=score_duration,
                detail={
                    "article_url_hash": url_hash,
                    "product_relevance": scores["product_relevance"],
                    "event_impact": scores["event_impact"],
                    "pr_total_score": scores["pr_total_score"],
                },
            )

            if scores.get("is_pr_candidate"):
                draft_started = time.perf_counter()
                await update_progress("draft", 2, "正在生成个性化草稿...")
                await log_pipeline(
                    db,
                    "INFO",
                    "draft",
                    "single article draft generation started",
                    user_id=user_id,
                    username=username,
                    trace_id=trace_id,
                    action="start",
                    detail={"article_url_hash": url_hash},
                )
                draft_gen = getattr(app.state, "draft_gen", None)
                if draft_gen:
                    style_hints = await load_style_hints(db, user_id)
                    log.info(
                        "[run-v2-single] style_hints injected=%s user_id=%s",
                        bool(style_hints),
                        user_id,
                    )
                    drafts = await draft_gen.generate(
                        dict(article),
                        scores,
                        style_hints=style_hints,
                    )
                    if drafts["ok"]:
                        now = datetime.now(UTC)
                        await db["user_drafts"].update_one(
                            {"user_id": user_id, "article_url_hash": url_hash},
                            {
                                "$set": {"drafts": drafts["drafts"], "updated_at": now},
                                "$setOnInsert": {"created_at": now},
                            },
                            upsert=True,
                        )
                    result["steps"].append(
                        {
                            "phase": "draft",
                            "draft_count": len(drafts["drafts"]),
                            "templates": list({draft["template"] for draft in drafts["drafts"]}),
                        }
                    )
                    log.info("[run-v2-single] Drafts: %s generated", len(drafts["drafts"]))
                    draft_duration = int((time.perf_counter() - draft_started) * 1000)
                    await log_pipeline(
                        db,
                        "INFO",
                        "draft",
                        "single article drafts generated",
                        user_id=user_id,
                        username=username,
                        trace_id=trace_id,
                        action="complete",
                        duration_ms=draft_duration,
                        detail={
                            "article_url_hash": url_hash,
                            "draft_count": len(drafts["drafts"]),
                            "templates": list({draft["template"] for draft in drafts["drafts"]}),
                            "style_hints_used": bool(style_hints),
                        },
                    )

    completed_phases = {step["phase"] for step in result["steps"]}
    if "score_v2" not in completed_phases:
        await log_pipeline(
            db,
            "INFO",
            "score_v2",
            "single article scoring skipped",
            user_id=user_id,
            username=username,
            trace_id=trace_id,
            action="skip",
            detail={"article_url_hash": url_hash, "reason": "not_pr_eligible_or_scorer_missing"},
        )
    if "draft" not in completed_phases:
        await log_pipeline(
            db,
            "INFO",
            "draft",
            "single article draft generation skipped",
            user_id=user_id,
            username=username,
            trace_id=trace_id,
            action="skip",
            detail={"article_url_hash": url_hash, "reason": "not_candidate_or_generator_missing"},
        )
    result["trace_id"] = trace_id
    result["ok"] = True
    return result


@router.post("/score-v2", summary="V2 双维度打分（批量）")
async def score_v2_all(request: Request, user_id: str = Depends(get_current_user)):
    """对所有 is_pr_eligible=True 的文章进行 V2 双维度打分。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    scorer = getattr(request.app.state, "scorer_v2", None)
    if scorer is None:
        raise HTTPException(status_code=503, detail="ScoringAgentV2 not initialized")

    log = logging.getLogger("backend.api.pipeline")

    try:
        cursor = db["articles"].find(
            {
                "is_pr_eligible": True,
                "pr_total_score": None,
            }
        )
        articles = await cursor.to_list(length=500)

        if not articles:
            return {"ok": True, "total": 0, "scored": 0}

        scored = await scorer.score_batch(articles, user_id=user_id)
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
                        }
                    },
                )
                scored_count += 1
                if result.get("is_pr_candidate"):
                    candidates += 1

        log.info(f"[score-v2] Scored: {scored_count}/{len(articles)}, {candidates} PR candidates")
        return {
            "ok": True,
            "total": len(articles),
            "scored": scored_count,
            "candidates": candidates,
        }

    except Exception as e:
        log.error(f"[score-v2] Failed: {e}")
        raise HTTPException(status_code=502, detail=str(e)) from e


def _score_payload(article: dict, *, skipped: bool) -> dict:
    total = article.get("pr_total_score")
    is_candidate = article.get("is_pr_candidate")
    if is_candidate is None:
        is_candidate = isinstance(total, (int, float)) and total >= 80
    return {
        "ok": True,
        "product_relevance": article.get("product_relevance", 0),
        "event_impact": article.get("event_impact", 0),
        "pr_total_score": total,
        "is_pr_candidate": is_candidate,
        "skipped": skipped,
    }


@router.post("/score-v2/{url_hash}", summary="V2 双维度打分（单篇）")
async def score_v2_single(
    url_hash: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """幂等打分单篇文章，并用文章级短期锁避免并发重复调用 LLM。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    scorer = getattr(request.app.state, "scorer_v2", None)
    if scorer is None:
        raise HTTPException(status_code=503, detail="ScoringAgentV2 not initialized")

    log = logging.getLogger("backend.api.pipeline")
    trace_id = generate_trace_id()

    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    if article.get("pr_total_score") is not None:
        await _log_idempotent_skip(
            db, request, user_id, "score_v2", url_hash, trace_id, "already_scored"
        )
        return _score_payload(article, skipped=True)

    lock_key = f"score-v2:{url_hash}"
    acquired = await acquire_pipeline_lock(
        db,
        lock_key,
        user_id,
        lock_type="score",
    )
    if not acquired:
        status = await wait_for_pipeline_lock(db, lock_key)
        if status == "timeout":
            raise _pipeline_timeout_error()
        article = await db["articles"].find_one({"url_hash": url_hash})
        if article and article.get("pr_total_score") is not None:
            await _log_idempotent_skip(
                db, request, user_id, "score_v2", url_hash, trace_id, "concurrent_result"
            )
            return _score_payload(article, skipped=True)
        acquired = await acquire_pipeline_lock(
            db,
            lock_key,
            user_id,
            lock_type="score",
        )
        if not acquired:
            raise _pipeline_timeout_error()

    try:
        article = await db["articles"].find_one({"url_hash": url_hash})
        if article and article.get("pr_total_score") is not None:
            await release_pipeline_lock(db, lock_key, success=True)
            await _log_idempotent_skip(
                db, request, user_id, "score_v2", url_hash, trace_id, "locked_recheck"
            )
            return _score_payload(article, skipped=True)

        scores = await scorer.score_single(
            dict(article),
            user_id=user_id,
            trace_id=trace_id,
        )
        scored = {
            "product_relevance": scores["product_relevance"],
            "event_impact": scores["event_impact"],
            "pr_total_score": scores["pr_total_score"],
            "score_reason": scores.get("score_reason", ""),
            "is_pr_candidate": scores.get("is_pr_candidate", False),
        }
        await db["articles"].update_one(
            {"url_hash": url_hash, "pr_total_score": None},
            {"$set": scored},
        )
    except Exception as exc:
        await release_pipeline_lock(db, lock_key, success=False)
        log.error("[score-v2-single] Failed: %s", exc)
        await log_pipeline(
            db,
            "ERROR",
            "score_v2",
            "single article scoring failed",
            user_id=user_id,
            username=_request_username(request, user_id),
            trace_id=trace_id,
            action="error",
            error=build_log_error(exc),
            detail={"article_url_hash": url_hash},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await release_pipeline_lock(db, lock_key, success=True)
    log.info(
        "[score-v2-single] %s+%s=%s",
        scored["product_relevance"],
        scored["event_impact"],
        scored["pr_total_score"],
    )
    return _score_payload(scored, skipped=False)


def _serialize_pipeline_task(document: dict) -> dict:
    """将 MongoDB 任务文档转换为可 JSON 序列化的响应。"""
    result = dict(document)
    object_id = result.pop("_id", None)
    if object_id is not None:
        result["id"] = str(object_id)
    return result


@router.get("/tasks/{task_id}/checkpoints", summary="查询流水线任务检查点")
async def list_task_checkpoints(
    task_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Return decoded LangGraph checkpoints for a task owned by the caller."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    task = await _get_owned_pipeline_task(db, task_id, user_id)
    checkpoints = await _read_task_checkpoints(db, task, limit=limit)
    return {
        "ok": True,
        "data": {
            "task_id": task_id,
            "thread_id": task.get("thread_id") or f"thread-{task_id}",
            "checkpoints": checkpoints,
        },
    }


@router.post("/tasks/{task_id}/resume", summary="从最后检查点恢复流水线任务")
async def resume_pipeline_task(
    task_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Validate ownership and enqueue checkpoint recovery in the ARQ worker."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    task = await _get_owned_pipeline_task(db, task_id, user_id)
    if task.get("task_type") != "run-v2" or task.get("status") not in {
        "failed",
        "cancelled",
        "interrupted",
    }:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TASK_NOT_RESUMABLE",
                "message": "当前任务状态不允许恢复",
            },
        )

    checkpoints = await _read_task_checkpoints(db, task, limit=1)
    if not checkpoints:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CHECKPOINT_NOT_FOUND",
                "message": "未找到可恢复的检查点",
            },
        )

    pool = getattr(request.app.state, "arq_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Task queue not available")

    progress = {
        "phase": "resume_pending",
        "current": 0,
        "total": 8,
        "message": "恢复任务排队中...",
    }
    now = datetime.now(UTC)
    claim = await db["pipeline_tasks"].update_one(
        {
            "task_id": task_id,
            "user_id": user_id,
            "status": {"$in": ["failed", "cancelled", "interrupted"]},
        },
        {
            "$set": {
                "status": "pending",
                "progress": progress,
                "error": None,
                "retry_count": 0,
                "resume_requested_at": now,
                "updated_at": now,
                "expires_at": now + timedelta(hours=2),
            }
        },
    )
    if claim.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TASK_NOT_RESUMABLE",
                "message": "任务已被其他恢复请求处理",
            },
        )
    try:
        job = await pool.enqueue_job(
            "resume_pipeline",
            task_id=task_id,
            user_id=user_id,
            _job_id=f"resume-{task_id}-{uuid4().hex[:8]}",
        )
    except Exception as exc:
        await PipelineStateManager(db).update_status(
            task_id,
            "failed",
            error=f"Resume queue unavailable: {exc}",
        )
        raise HTTPException(status_code=503, detail="Task queue unavailable") from exc
    if job is None:
        await PipelineStateManager(db).update_status(
            task_id,
            "failed",
            error="Resume task is already queued",
        )
        raise HTTPException(status_code=409, detail="Resume task is already queued")

    resumed_from = checkpoints[0].get("node") or task.get("last_node", "")
    return {
        "ok": True,
        "data": {
            "task_id": task_id,
            "message": "任务已从检查点恢复，重新入队",
            "resumed_from": resumed_from,
        },
    }


@router.get("/tasks/{task_id}", summary="查询流水线任务状态")
async def get_pipeline_task(
    task_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """按任务 ID 查询状态；访问其他用户任务返回 403。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    task = await _get_owned_pipeline_task(db, task_id, user_id)
    return {"ok": True, "data": _serialize_pipeline_task(task)}


@router.get("/tasks", summary="查询当前用户流水线任务列表")
async def list_pipeline_tasks(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user_id: str = Depends(get_current_user),
):
    """分页返回当前用户的任务，按创建时间倒序排列。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    query = {"user_id": user_id}
    total = await db["pipeline_tasks"].count_documents(query)
    cursor = (
        db["pipeline_tasks"]
        .find(query)
        .sort("created_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    tasks = await cursor.to_list(length=page_size)
    return {
        "ok": True,
        "data": {
            "items": [_serialize_pipeline_task(task) for task in tasks],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    }


def _serialize_llm_log(document: dict[str, Any]) -> dict[str, Any]:
    """Convert MongoDB-specific values in an LLM call log to JSON values."""
    result = {key: value for key, value in document.items() if key != "_id"}
    for field in ("created_at", "expires_at"):
        if isinstance(result.get(field), datetime):
            result[field] = result[field].isoformat()
    return result


@llm_router.get("/llm-logs", summary="查询当前用户的 LLM 调用记录")
async def list_llm_logs(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    agent_type: str | None = Query(default=None, max_length=50),
    task_id: str | None = Query(default=None, max_length=100),
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Return tenant-isolated LLM metadata without storing or exposing prompts."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    query: dict[str, Any] = {"user_id": user_id}
    if agent_type:
        query["agent_type"] = agent_type
    if task_id:
        query["task_id"] = task_id

    collection = db["llm_call_logs"]
    total = await collection.count_documents(query)
    cursor = (
        collection.find(query).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)
    )
    documents = await cursor.to_list(length=page_size)
    return {
        "ok": True,
        "data": {
            "items": [_serialize_llm_log(document) for document in documents],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    }
