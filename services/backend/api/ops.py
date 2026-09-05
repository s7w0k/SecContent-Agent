"""Ops 观测出口 — 面向开发者/运维的只读指标快照（P3）。

当前提供进程无关的 MongoDB 计数快照（任务状态分布、用户/反馈/LLM 调用量）；
Prometheus/OTel 拉取可基于同一数据源增加独立端点（部署层按需接入）。
仅 is_developer 可访问，避免把跨用户运行规模暴露给普通账号。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from auth.deps import get_developer_user
from fastapi import APIRouter, Depends, HTTPException, Request

router = APIRouter(prefix="/api/ops", tags=["Ops"])

_STATUSES = ("pending", "running", "resume_pending", "failed", "completed", "cancelled")

_TASK_STATUS_QUERY = {"$in": list(_STATUSES)}


async def collect_metrics(db: Any) -> dict[str, Any]:
    """采集一份只读指标快照（单集合计数均容忍失败，失败记为 None）。"""

    async def count(name: str, query: dict | None = None) -> int | None:
        try:
            return int(await db[name].count_documents(query or {}))
        except Exception:
            return None

    task_by_status: dict[str, int | None] = {}
    for status in _STATUSES:
        task_by_status[status] = await count("pipeline_tasks", {"status": status})

    active = sum(
        (task_by_status.get(s) or 0) for s in ("pending", "running", "resume_pending")
    )
    return {
        "tasks": {
            "by_status": task_by_status,
            "active": active,
        },
        "users": await count("users"),
        "articles": await count("articles"),
        "feedbacks": await count("feedbacks"),
        "llm_call_logs": await count("llm_call_logs"),
    }


@router.get("/metrics", summary="只读运维指标快照（开发者权限）")
async def ops_metrics(request: Request, _dev: Any = Depends(get_developer_user)) -> dict[str, Any]:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "metrics": await collect_metrics(db),
    }
