"""用户反馈 REST API。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from agent.template_compat import template_reference
from api.activity import log_activity
from api.logs import generate_trace_id, log_pipeline
from auth.deps import get_current_user
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from logging_config import get_audit_logger
from models.feedback import (
    Feedback,
    FeedbackCreate,
    FeedbackStatus,
    FeedbackUpdate,
    TargetType,
)

router = APIRouter(prefix="/api/feedback", tags=["Feedback"])


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
    for key in ("created_at", "updated_at"):
        value = result.get(key)
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def _locate_draft(article: dict, draft_index: int | None) -> dict:
    """按数组位置定位草稿，不存在时返回 404。"""
    drafts = article.get("pr_drafts", [])
    if draft_index is None or draft_index >= len(drafts):
        raise HTTPException(status_code=404, detail="Draft not found")
    return drafts[draft_index]


def _validate_feedback_target(body: FeedbackCreate, article: dict) -> dict | None:
    """校验反馈对象，并返回关联草稿。"""
    if body.target_type not in {TargetType.DRAFT, TargetType.REVISION}:
        return None

    draft = _locate_draft(article, body.target_ref.draft_index)
    if body.target_type == TargetType.REVISION:
        revision_id = body.target_ref.revision_id
        if not revision_id:
            raise HTTPException(status_code=422, detail="revision_id is required")
        revisions = draft.get("revisions", [])
        if not any(item.get("revision_id") == revision_id for item in revisions):
            raise HTTPException(status_code=404, detail="Revision not found")
    return draft


async def _feedback_documents(db, query: dict) -> list[dict]:
    """读取符合条件的反馈文档。"""
    cursor = db["feedbacks"].find(query)
    return await cursor.to_list(length=None)


async def _recalculate_draft_feedback_summary(
    db,
    user_id: str,
    article_url_hash: str,
    draft_index: int | None,
) -> None:
    """重新计算单篇草稿的反馈冗余汇总。"""
    if draft_index is None:
        return

    user_draft = await db["user_drafts"].find_one(
        {"user_id": user_id, "article_url_hash": article_url_hash}
    )
    if user_draft is None:
        return

    drafts = user_draft.get("drafts", [])
    if draft_index >= len(drafts):
        return

    feedbacks = await _feedback_documents(
        db,
        {
            "user_id": user_id,
            "status": FeedbackStatus.ACTIVE,
            "target_type": {"$in": [TargetType.DRAFT, TargetType.REVISION]},
            "target_ref.article_url_hash": article_url_hash,
            "target_ref.draft_index": draft_index,
        },
    )
    feedbacks.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    ratings = [item["rating"] for item in feedbacks]
    drafts[draft_index]["feedback_summary"] = {
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
        "count": len(ratings),
        "last_rating": ratings[0] if ratings else None,
    }
    await db["user_drafts"].update_one(
        {"user_id": user_id, "article_url_hash": article_url_hash},
        {"$set": {"drafts": drafts, "updated_at": datetime.now(UTC)}},
    )


async def _log_feedback_activity(
    db,
    feedback: Feedback,
    article: dict,
    draft: dict | None,
) -> None:
    """写入 feedback_submit 操作记录。"""
    template = template_reference(draft)
    await log_activity(
        db,
        feedback.user_id,
        "feedback_submit",
        {
            "article_url_hash": feedback.target_ref.article_url_hash,
            "draft_index": feedback.target_ref.draft_index,
            "template": draft.get("template") if draft else None,
            **template,
            "perspective": draft.get("perspective") if draft else None,
            "revision_id": feedback.target_ref.revision_id,
        },
        {
            "article_title": article.get("title", ""),
            "category_v2": article.get("category_v2", ""),
            "pr_total_score": article.get("pr_total_score", 0),
            "target_type": feedback.target_type,
            "rating": feedback.rating,
        },
        {"feedback_id": feedback.feedback_id},
    )


@router.post("", summary="提交反馈")
async def create_feedback(
    request: Request,
    body: FeedbackCreate,
    user_id: str = Depends(get_current_user),
):
    """提交反馈并同步草稿汇总与操作记录。"""
    db = _get_db(request)
    trace_id = generate_trace_id()
    article = await db["articles"].find_one({"url_hash": body.target_ref.article_url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    user_draft = await db["user_drafts"].find_one(
        {"user_id": user_id, "article_url_hash": body.target_ref.article_url_hash}
    )
    draft_source = {"pr_drafts": user_draft.get("drafts", []) if user_draft else []}
    draft = _validate_feedback_target(body, draft_source)
    reference = template_reference(draft)
    feedback = Feedback(
        **body.model_dump(),
        user_id=user_id,
        **reference,
        perspective=draft.get("perspective") if draft else None,
    )
    await db["feedbacks"].insert_one(
        feedback.model_dump(exclude={"id"}, mode="python"),
    )

    await _recalculate_draft_feedback_summary(
        db,
        user_id,
        feedback.target_ref.article_url_hash,
        feedback.target_ref.draft_index,
    )
    await _log_feedback_activity(db, feedback, article, draft)
    await log_pipeline(
        db,
        "INFO",
        "feedback_submit",
        "feedback submitted",
        user_id=user_id,
        username=getattr(getattr(request, "state", None), "username", None) or user_id,
        trace_id=trace_id,
        action="complete",
        detail={
            "feedback_id": feedback.feedback_id,
            "target_type": feedback.target_type,
            "target_ref": {
                "article_url_hash": feedback.target_ref.article_url_hash,
                "draft_index": feedback.target_ref.draft_index,
                "revision_id": feedback.target_ref.revision_id,
            },
            "rating": feedback.rating,
            "tags": feedback.tags,
            **reference,
        },
    )
    get_audit_logger().log(
        user_id=user_id,
        action="feedback_submit",
        detail={"rating": feedback.rating, "tags": feedback.tags},
    )

    # 双写记忆事件（Feature Flag 控制）
    from agent.memory_event_service import create_memory_event
    from models.memory import MemorySourceType

    source_type = (
        MemorySourceType.FEEDBACK_COMMENT
        if feedback.comment
        else MemorySourceType.FEEDBACK_RATING
    )
    await create_memory_event(
        db,
        user_id,
        source_type,
        source_id=feedback.feedback_id,
        article_url_hash=feedback.target_ref.article_url_hash,
        draft_index=feedback.target_ref.draft_index,
        category_v2=article.get("category_v2") if article else None,
        payload={
            "rating": feedback.rating,
            "comment": (feedback.comment or "")[:500],
            "tags": feedback.tags or [],
        },
        idempotency_key=f"feedback:{feedback.feedback_id}",
    )

    return {
        "ok": True,
        "data": {
            "feedback_id": feedback.feedback_id,
            "created_at": feedback.created_at.isoformat(),
            "trace_id": trace_id,
        },
    }


@router.get("", summary="查询反馈")
async def list_feedback(
    request: Request,
    target_type: TargetType | None = Query(default=None),
    article_url_hash: str | None = Query(default=None),
    draft_index: int | None = Query(default=None, ge=0),
    status: FeedbackStatus = Query(default=FeedbackStatus.ACTIVE),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user_id: str = Depends(get_current_user),
):
    """按反馈对象、文章和草稿筛选反馈。"""
    db = _get_db(request)
    query: dict = {"user_id": user_id, "status": status}
    if target_type is not None:
        query["target_type"] = target_type
    if article_url_hash:
        query["target_ref.article_url_hash"] = article_url_hash
    if draft_index is not None:
        query["target_ref.draft_index"] = draft_index

    documents = await _feedback_documents(db, query)
    documents.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    total = len(documents)
    ratings = [item["rating"] for item in documents]
    start = (page - 1) * page_size
    items = documents[start : start + page_size]

    return {
        "ok": True,
        "data": {
            "items": [_serialize_document(item) for item in items],
            "total": total,
            "avg_rating": round(sum(ratings) / total, 2) if total else 0,
            "page": page,
            "page_size": page_size,
        },
    }


@router.get("/stats", summary="反馈统计")
async def feedback_stats(
    request: Request,
    group_by: Literal["template", "perspective"] = Query(default="template"),
    user_id: str = Depends(get_current_user),
):
    """按草稿模板或视角统计反馈数量和平均分。"""
    db = _get_db(request)
    feedbacks = await _feedback_documents(
        db,
        {
            "user_id": user_id,
            "status": FeedbackStatus.ACTIVE,
            "target_type": {"$in": [TargetType.DRAFT, TargetType.REVISION]},
        },
    )

    draft_cache: dict[str, dict | None] = {}
    grouped: dict[str, list[int]] = {}
    for feedback in feedbacks:
        target_ref = feedback.get("target_ref", {})
        article_hash = target_ref.get("article_url_hash")
        draft_index = target_ref.get("draft_index")
        if not article_hash or draft_index is None:
            continue
        if article_hash not in draft_cache:
            draft_cache[article_hash] = await db["user_drafts"].find_one(
                {"user_id": user_id, "article_url_hash": article_hash}
            )
        user_draft = draft_cache[article_hash]
        if user_draft is None:
            continue
        drafts = user_draft.get("drafts", [])
        if draft_index < 0 or draft_index >= len(drafts):
            continue
        key = drafts[draft_index].get(group_by) or "未标注"
        grouped.setdefault(key, []).append(feedback["rating"])

    groups = [
        {
            "key": key,
            "count": len(ratings),
            "avg_rating": round(sum(ratings) / len(ratings), 2),
        }
        for key, ratings in grouped.items()
    ]
    groups.sort(key=lambda item: (-item["avg_rating"], -item["count"], item["key"]))
    all_ratings = [rating for ratings in grouped.values() for rating in ratings]

    return {
        "ok": True,
        "data": {
            "groups": groups,
            "total": len(all_ratings),
            "overall_avg": (round(sum(all_ratings) / len(all_ratings), 2) if all_ratings else 0),
        },
    }


@router.put("/{feedback_id}", summary="更新反馈")
async def update_feedback(
    feedback_id: str,
    request: Request,
    body: FeedbackUpdate,
    user_id: str = Depends(get_current_user),
):
    """更新反馈内容并重新计算草稿汇总。"""
    db = _get_db(request)
    current = await db["feedbacks"].find_one({"feedback_id": feedback_id, "user_id": user_id})
    if current is None:
        raise HTTPException(status_code=404, detail="Feedback not found")

    updates = body.model_dump(exclude_unset=True, mode="python")
    updates["updated_at"] = datetime.now(UTC)
    await db["feedbacks"].update_one(
        {"feedback_id": feedback_id, "user_id": user_id},
        {"$set": updates},
    )

    target_ref = current.get("target_ref", {})
    await _recalculate_draft_feedback_summary(
        db,
        user_id,
        target_ref.get("article_url_hash", ""),
        target_ref.get("draft_index"),
    )
    return {
        "ok": True,
        "data": {
            "feedback_id": feedback_id,
            "updated": True,
            "updated_at": updates["updated_at"].isoformat(),
        },
    }


@router.delete("/{feedback_id}", summary="删除反馈")
async def delete_feedback(
    feedback_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """删除反馈并重新计算草稿汇总。"""
    db = _get_db(request)
    current = await db["feedbacks"].find_one({"feedback_id": feedback_id, "user_id": user_id})
    if current is None:
        raise HTTPException(status_code=404, detail="Feedback not found")

    result = await db["feedbacks"].delete_one({"feedback_id": feedback_id, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Feedback not found")

    target_ref = current.get("target_ref", {})
    await _recalculate_draft_feedback_summary(
        db,
        user_id,
        target_ref.get("article_url_hash", ""),
        target_ref.get("draft_index"),
    )
    return {"ok": True, "data": {"feedback_id": feedback_id, "deleted": True}}
