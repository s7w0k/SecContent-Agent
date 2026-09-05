"""用户操作记录 REST API 与统一埋点辅助函数。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from auth.deps import get_current_user
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from models.feedback import ActionType, UserActivity, UserActivityCreate
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/activities", tags=["Activity"])
logger = logging.getLogger("backend.api.activity")


class ActivityBatchCreate(BaseModel):
    """批量操作记录请求。"""

    activities: list[UserActivityCreate] = Field(..., min_length=1, max_length=100)


def _get_db(request: Request):
    """从 app.state 获取 MongoDB 数据库实例。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


def _serialize_document(document: dict) -> dict:
    """将 MongoDB 文档转换为 JSON 可序列化结构。"""
    result = dict(document)
    if "_id" in result:
        result["_id"] = str(result["_id"])
    created_at = result.get("created_at")
    if isinstance(created_at, datetime):
        result["created_at"] = created_at.isoformat()
    return result


async def _activity_documents(db, query: dict) -> list[dict]:
    """读取符合条件的操作记录。"""
    cursor = db["user_activities"].find(query)
    return await cursor.to_list(length=None)


async def log_activity(
    db,
    user_id: str,
    action: ActionType | str,
    target: dict,
    context: dict | None = None,
    metadata: dict | None = None,
) -> str | None:
    """尽力写入一条操作记录，失败时只记录 warning。"""
    try:
        activity = UserActivity(
            user_id=user_id,
            action=action,
            target=target,
            context=context or {},
            metadata=metadata or {},
        )
        await db["user_activities"].insert_one(
            activity.model_dump(exclude={"id"}, mode="python"),
        )
        return activity.activity_id
    except Exception as exc:
        logger.warning("Failed to log activity: %s", exc)
        return None


@router.post("/log", summary="记录单条操作")
async def create_activity(
    request: Request,
    body: UserActivityCreate,
    user_id: str = Depends(get_current_user),
):
    """写入一条前端异步埋点记录。"""
    db = _get_db(request)
    activity_id = await log_activity(
        db,
        user_id,
        body.action,
        body.target.model_dump(mode="python"),
        body.context,
        body.metadata,
    )
    if activity_id is None:
        raise HTTPException(status_code=500, detail="Activity log failed")
    return {
        "ok": True,
        "data": {
            "activity_id": activity_id,
            "created_at": datetime.now(UTC).isoformat(),
        },
    }


@router.post("/batch-log", summary="批量记录操作")
async def create_activity_batch(
    request: Request,
    body: ActivityBatchCreate,
    user_id: str = Depends(get_current_user),
):
    """一次写入多条前端操作记录。"""
    db = _get_db(request)
    activity_ids: list[str] = []
    for item in body.activities:
        activity_id = await log_activity(
            db,
            user_id,
            item.action,
            item.target.model_dump(mode="python"),
            item.context,
            item.metadata,
        )
        if activity_id is not None:
            activity_ids.append(activity_id)

    if not activity_ids:
        raise HTTPException(status_code=500, detail="Activity batch log failed")
    return {
        "ok": True,
        "data": {
            "activity_ids": activity_ids,
            "recorded": len(activity_ids),
            "failed": len(body.activities) - len(activity_ids),
        },
    }


@router.get("", summary="查询操作记录")
async def list_activities(
    request: Request,
    action: ActionType | None = Query(default=None),
    article_url_hash: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user_id: str = Depends(get_current_user),
):
    """按操作类型和文章筛选操作记录并分页。"""
    db = _get_db(request)
    query: dict = {"user_id": user_id}
    if action is not None:
        query["action"] = action
    if article_url_hash:
        query["target.article_url_hash"] = article_url_hash

    documents = await _activity_documents(db, query)
    documents.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    total = len(documents)
    start = (page - 1) * page_size
    items = documents[start : start + page_size]
    return {
        "ok": True,
        "data": {
            "items": [_serialize_document(item) for item in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    }


@router.get("/stats", summary="操作记录统计")
async def activity_stats(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    user_id: str = Depends(get_current_user),
):
    """统计指定时间范围内的操作类型、模板和每日趋势。"""
    db = _get_db(request)
    start_at = datetime.now(UTC) - timedelta(days=days)
    documents = await _activity_documents(
        db,
        {
            "user_id": user_id,
            "created_at": {"$gte": start_at},
        },
    )

    by_action: dict[str, int] = {}
    by_template: dict[str, int] = {}
    daily_counts: dict[str, int] = {}
    for document in documents:
        action = str(document.get("action", "unknown"))
        by_action[action] = by_action.get(action, 0) + 1

        template = document.get("target", {}).get("template")
        if template:
            by_template[template] = by_template.get(template, 0) + 1

        created_at = document.get("created_at")
        if isinstance(created_at, datetime):
            date_key = created_at.date().isoformat()
        else:
            date_key = str(created_at)[:10]
        if date_key:
            daily_counts[date_key] = daily_counts.get(date_key, 0) + 1

    return {
        "ok": True,
        "data": {
            "total": len(documents),
            "by_action": by_action,
            "by_template": by_template,
            "daily_trend": [
                {"date": date, "count": count} for date, count in sorted(daily_counts.items())
            ],
        },
    }
