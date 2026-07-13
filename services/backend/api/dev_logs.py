"""开发者跨用户日志查询 API。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from api.logs import LOG_COLLECTION
from auth.deps import get_developer_user
from fastapi import APIRouter, Depends, HTTPException, Query, Request

router = APIRouter(prefix="/api/dev/logs", tags=["Developer Logs"])


def _tz() -> timezone:
    return timezone(timedelta(hours=8))


def _get_db(request: Request) -> Any:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


def _serialize_document(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    if "_id" in result:
        result["_id"] = str(result["_id"])
    created_at = result.get("created_at")
    if isinstance(created_at, datetime):
        result["created_at"] = created_at.isoformat()
    return result


def _multi_value(value: str | None) -> list[str]:
    if not value:
        return []
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _date_query(date: str | None) -> dict[str, Any]:
    return {"date": date or datetime.now(_tz()).strftime("%Y-%m-%d")}


@router.get("")
async def query_logs(
    request: Request,
    date: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    level: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _developer: tuple[str, dict[str, Any]] = Depends(get_developer_user),
) -> dict[str, Any]:
    """跨用户、多维筛选并分页查询日志。"""
    db = _get_db(request)
    collection = db[LOG_COLLECTION]
    query = _date_query(date)
    if user_id:
        query["user_id"] = user_id
    phases = _multi_value(phase)
    if phases:
        query["phase"] = {"$in": phases}
    levels = _multi_value(level)
    if levels:
        query["level"] = {"$in": levels}
    if trace_id:
        query["trace_id"] = trace_id
    if keyword:
        query["message"] = {"$regex": keyword, "$options": "i"}

    total = await collection.count_documents(query)
    cursor = collection.find(query).sort("created_at", -1)
    cursor = cursor.skip((page - 1) * page_size).limit(page_size)
    documents = await cursor.to_list(length=page_size)

    option_query = _date_query(date)
    option_phases = await collection.distinct("phase", option_query)
    option_levels = await collection.distinct("level", option_query)
    user_cursor = collection.aggregate(
        [
            {"$match": option_query},
            {"$group": {"_id": "$user_id", "username": {"$last": "$username"}}},
            {"$sort": {"username": 1, "_id": 1}},
        ]
    )
    users = [
        {"user_id": item["_id"], "username": item.get("username") or item["_id"]}
        async for item in user_cursor
        if item.get("_id")
    ]
    return {
        "ok": True,
        "data": {
            "logs": [_serialize_document(document) for document in documents],
            "phases": sorted(option_phases),
            "levels": sorted(option_levels),
            "users": users,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    }


@router.get("/dates")
async def list_dates(
    request: Request,
    _developer: tuple[str, dict[str, Any]] = Depends(get_developer_user),
) -> dict[str, Any]:
    """返回所有租户日志的日期列表。"""
    dates = await _get_db(request)[LOG_COLLECTION].distinct("date")
    return {"ok": True, "data": {"dates": sorted(dates, reverse=True)}}


@router.get("/stats")
async def get_stats(
    request: Request,
    date: str | None = Query(default=None),
    _developer: tuple[str, dict[str, Any]] = Depends(get_developer_user),
) -> dict[str, Any]:
    """统计指定日期的日志级别、阶段、用户与平均阶段耗时。"""
    collection = _get_db(request)[LOG_COLLECTION]
    query = _date_query(date)
    documents = await collection.find(query).to_list(length=None)
    by_level: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    users: dict[str, dict[str, Any]] = {}
    duration_totals: dict[str, int] = {}
    duration_counts: dict[str, int] = {}
    error_count = 0
    for document in documents:
        level = str(document.get("level") or "UNKNOWN")
        phase = str(document.get("phase") or "unknown")
        by_level[level] = by_level.get(level, 0) + 1
        by_phase[phase] = by_phase.get(phase, 0) + 1
        if level in {"ERROR", "CRITICAL"}:
            error_count += 1

        user_id = str(document.get("user_id") or "unknown")
        user = users.setdefault(
            user_id,
            {
                "user_id": user_id,
                "username": document.get("username") or user_id,
                "count": 0,
            },
        )
        user["count"] += 1
        if document.get("username"):
            user["username"] = document["username"]

        duration = document.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            duration_totals[phase] = duration_totals.get(phase, 0) + int(duration)
            duration_counts[phase] = duration_counts.get(phase, 0) + 1

    avg_duration_ms = {
        phase: round(total / duration_counts[phase]) for phase, total in duration_totals.items()
    }
    return {
        "ok": True,
        "data": {
            "total": len(documents),
            "by_level": by_level,
            "by_phase": by_phase,
            "by_user": sorted(users.values(), key=lambda item: (-item["count"], item["user_id"])),
            "error_count": error_count,
            "avg_duration_ms": avg_duration_ms,
        },
    }


@router.get("/trace/{trace_id}")
async def get_trace(
    trace_id: str,
    request: Request,
    _developer: tuple[str, dict[str, Any]] = Depends(get_developer_user),
) -> dict[str, Any]:
    """按时间正序返回一条完整调用链。"""
    cursor = _get_db(request)[LOG_COLLECTION].find({"trace_id": trace_id})
    events = await cursor.sort("created_at", 1).to_list(length=1000)
    if not events:
        raise HTTPException(
            status_code=404,
            detail={"code": "TRACE_NOT_FOUND", "message": "链路不存在"},
        )

    total_duration_ms = sum(
        int(event["duration_ms"])
        for event in events
        if isinstance(event.get("duration_ms"), (int, float))
        and not isinstance(event.get("duration_ms"), bool)
    )
    phases = {event.get("phase") for event in events if event.get("phase")}
    first = events[0]
    return {
        "ok": True,
        "data": {
            "trace_id": trace_id,
            "user_id": first.get("user_id"),
            "username": first.get("username") or first.get("user_id"),
            "events": [_serialize_document(event) for event in events],
            "total_duration_ms": total_duration_ms,
            "phase_count": len(phases),
            "has_error": any(event.get("level") in {"ERROR", "CRITICAL"} for event in events),
        },
    }
