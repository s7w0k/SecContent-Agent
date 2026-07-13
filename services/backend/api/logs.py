"""Operational logs API with date-based filtering."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from auth.deps import get_current_user
from fastapi import APIRouter, Depends, Query, Request
from models.feedback import PipelineLog

router = APIRouter(prefix="/api/logs", tags=["Logs"])

LOG_COLLECTION = "pipeline_logs"
logger = logging.getLogger("backend.api.logs")


def _tz() -> timezone:
    return timezone(timedelta(hours=8))


def generate_trace_id() -> str:
    """生成可读的链路 ID。"""

    return f"trace-{datetime.now(_tz()).strftime('%Y%m%d')}-{uuid4().hex[:12]}"


async def _log_to_db(
    db: Any,
    level: str,
    phase: str,
    message: str,
    user_id: str,
    detail: dict[str, Any] | None = None,
    *,
    trace_id: str | None = None,
    action: str = "complete",
    duration_ms: int | None = None,
    error: dict[str, Any] | None = None,
    username: str | None = None,
) -> None:
    """Write a log entry to MongoDB."""
    if db is None:
        return
    try:
        now = datetime.now(_tz())
        log = PipelineLog(
            trace_id=trace_id or generate_trace_id(),
            user_id=user_id,
            username=username or user_id,
            level=level,
            phase=phase,
            action=action,
            message=message,
            detail=detail or {},
            duration_ms=duration_ms,
            error=error,
            created_at=now,
            date=now.strftime("%Y-%m-%d"),
        )
        await db[LOG_COLLECTION].insert_one(
            log.model_dump(exclude={"id"}, exclude_none=True, mode="python")
        )
    except Exception as exc:
        logger.warning("log_pipeline 写入失败: phase=%s, error=%s", phase, exc)


def log_pipeline(
    db: Any,
    level: str,
    phase: str,
    message: str,
    *,
    user_id: str,
    trace_id: str | None = None,
    action: str = "complete",
    duration_ms: int | None = None,
    error: dict[str, Any] | None = None,
    username: str | None = None,
    detail: dict[str, Any] | None = None,
    **legacy_detail: Any,
) -> asyncio.Task[None] | None:
    """Helper to log pipeline events (non-blocking)."""
    try:
        loop = asyncio.get_running_loop()
        merged_detail = {**(detail or {}), **legacy_detail}
        return loop.create_task(
            _log_to_db(
                db,
                level,
                phase,
                message,
                user_id,
                merged_detail,
                trace_id=trace_id,
                action=action,
                duration_ms=duration_ms,
                error=error,
                username=username,
            )
        )
    except RuntimeError as exc:
        logger.warning("log_pipeline 调度失败: phase=%s, error=%s", phase, exc)
        return None


# ---- API Endpoints ----


@router.get("/dates")
async def list_dates(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """List dates that have logs for the current user."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"dates": []}
    dates = await db[LOG_COLLECTION].distinct("date", {"user_id": user_id})
    return {"dates": sorted(dates, reverse=True)}


@router.get("/{date}")
async def get_logs_by_date(
    date: str,
    request: Request,
    phase: str = Query(default="", description="Filter by phase"),
    limit: int = Query(default=200, le=500),
    user_id: str = Depends(get_current_user),
):
    """Get logs for a specific date, optionally filtered by phase."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"date": date, "logs": [], "phases": []}

    query = {"user_id": user_id, "date": date}
    if phase:
        query["phase"] = phase

    cursor = db[LOG_COLLECTION].find(query).sort("created_at", -1).limit(limit)
    logs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        logs.append(doc)
    logs.reverse()

    phases = await db[LOG_COLLECTION].distinct(
        "phase",
        {"user_id": user_id, "date": date},
    )

    return {"date": date, "logs": logs, "phases": sorted(phases)}
