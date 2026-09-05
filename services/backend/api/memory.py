"""用户记忆审计 REST API。

提供记忆列表、详情、审批、编辑、停用、删除和预览接口。
所有操作均以 user_id 作为查询前置条件（约束 C6: 多租户隔离）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.memory_compiler import MemorySummaryCompiler
from agent.memory_confidence import determine_status
from agent.memory_retriever import MemoryRetriever
from auth.deps import get_current_user
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from models.memory import MemoryStage, MemoryStatus
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/memory", tags=["Memory"])


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


def _serialize(doc: dict) -> dict:
    result = dict(doc)
    result.pop("_id", None)
    for key in (
        "created_at",
        "updated_at",
        "first_seen_at",
        "last_seen_at",
        "last_used_at",
        "expires_at",
    ):
        val = result.get(key)
        if isinstance(val, datetime):
            result[key] = val.isoformat()
    return result


# ── 列表 ──────────────────────────────────────────────


@router.get("/items", summary="查询用户记忆列表")
async def list_items(
    request: Request,
    user_id: str = Depends(get_current_user),
    status: str | None = Query(default=None, description="逗号分隔的状态"),
    dimension: str | None = None,
    category_v2: str | None = None,
    stage: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    db = _get_db(request)
    query: dict[str, Any] = {"user_id": user_id}

    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            query["status"] = statuses[0]
        else:
            query["status"] = {"$in": statuses}
    if dimension:
        query["dimension"] = dimension
    if category_v2:
        query["scope.category_v2"] = category_v2
    if stage:
        query["scope.stage"] = stage

    total = await db["user_memory_items"].count_documents(query)
    cursor = (
        db["user_memory_items"]
        .find(query)
        .sort("updated_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [_serialize(doc) async for doc in cursor]

    # 状态统计
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]
    stats_list = await db["user_memory_items"].aggregate(pipeline).to_list(length=20)
    status_stats = {s["_id"]: s["count"] for s in stats_list}
    pending_count = status_stats.get("pending_approval", 0)

    return {
        "ok": True,
        "data": {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "status_stats": status_stats,
            "pending_count": pending_count,
        },
    }


# ── 详情 ──────────────────────────────────────────────


@router.get("/items/{memory_id}", summary="查询记忆详情")
async def get_item(
    memory_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    doc = await db["user_memory_items"].find_one({"memory_id": memory_id, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="Memory item not found")
    return {"ok": True, "data": _serialize(doc)}


# ── 审批操作 ──────────────────────────────────────────


@router.post("/items/{memory_id}/approve", summary="确认记忆")
async def approve_item(
    memory_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    result = await db["user_memory_items"].update_one(
        {"memory_id": memory_id, "user_id": user_id},
        {
            "$set": {
                "status": MemoryStatus.ACTIVE.value,
                "confirmed_by_user": True,
                "created_by": "user",
                "updated_at": datetime.now(UTC),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Memory item not found")
    return {"ok": True}


@router.post("/items/{memory_id}/reject", summary="拒绝记忆")
async def reject_item(
    memory_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    result = await db["user_memory_items"].update_one(
        {"memory_id": memory_id, "user_id": user_id},
        {
            "$set": {
                "status": MemoryStatus.REJECTED.value,
                "updated_at": datetime.now(UTC),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Memory item not found")
    return {"ok": True}


@router.post("/items/{memory_id}/suppress", summary="停用记忆")
async def suppress_item(
    memory_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    result = await db["user_memory_items"].update_one(
        {"memory_id": memory_id, "user_id": user_id},
        {
            "$set": {
                "status": MemoryStatus.SUPPRESSED.value,
                "updated_at": datetime.now(UTC),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Memory item not found")
    return {"ok": True}


@router.post("/items/{memory_id}/activate", summary="恢复记忆")
async def activate_item(
    memory_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    doc = await db["user_memory_items"].find_one({"memory_id": memory_id, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="Memory item not found")
    if doc.get("status") == MemoryStatus.REJECTED.value:
        raise HTTPException(status_code=400, detail="Cannot activate a rejected memory")
    new_status = determine_status(doc.get("confidence", 0), doc.get("confirmed_by_user", False))
    await db["user_memory_items"].update_one(
        {"memory_id": memory_id},
        {"$set": {"status": new_status.value, "updated_at": datetime.now(UTC)}},
    )
    return {"ok": True, "data": {"status": new_status.value}}


# ── 编辑 ──────────────────────────────────────────────


class MemoryItemUpdate(BaseModel):
    display_text: str = Field(..., min_length=1, max_length=500)
    polarity: str | None = None


@router.put("/items/{memory_id}", summary="编辑记忆")
async def edit_item(
    memory_id: str,
    body: MemoryItemUpdate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    doc = await db["user_memory_items"].find_one({"memory_id": memory_id, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="Memory item not found")

    update: dict[str, Any] = {
        "display_text": body.display_text,
        "created_by": "user",
        "confirmed_by_user": True,
        "status": MemoryStatus.ACTIVE.value,
        "version": doc.get("version", 1) + 1,
        "updated_at": datetime.now(UTC),
    }
    if body.polarity in ("prefer", "avoid", "require"):
        update["polarity"] = body.polarity

    await db["user_memory_items"].update_one(
        {"memory_id": memory_id},
        {"$set": update},
    )
    return {"ok": True}


# ── 删除（软删除） ────────────────────────────────────


@router.delete("/items/{memory_id}", summary="删除记忆（软删除）")
async def delete_item(
    memory_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    result = await db["user_memory_items"].update_one(
        {"memory_id": memory_id, "user_id": user_id},
        {
            "$set": {
                "status": MemoryStatus.REJECTED.value,
                "updated_at": datetime.now(UTC),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Memory item not found")
    return {"ok": True}


# ── 手动重编译 ────────────────────────────────────────


@router.post("/recompile", summary="手动重编译记忆摘要")
async def recompile(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    compiler = MemorySummaryCompiler(db)
    result = await compiler.compile_user(user_id)
    return {"ok": True, "data": result}


# ── Memory Pack 预览 ─────────────────────────────────


class MemoryPreviewRequest(BaseModel):
    category_v2: str | None = None
    template_id: str | None = None
    stage: str = "draft"


@router.post("/preview", summary="预览 Memory Pack")
async def preview_pack(
    body: MemoryPreviewRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    retriever = MemoryRetriever(db)
    pack = await retriever.retrieve(
        user_id=user_id,
        category_v2=body.category_v2,
        template_id=body.template_id,
        stage=MemoryStage(body.stage) if body.stage else MemoryStage.DRAFT,
    )
    return {
        "ok": True,
        "data": {
            "hard_preferences": pack.hard_preferences,
            "soft_preferences": [s.model_dump() for s in pack.soft_preferences],
            "avoid_patterns": pack.avoid_patterns,
            "rendered_text": pack.rendered_text,
            "char_count": pack.char_count,
            "item_count": pack.item_count,
            "pruned_count": pack.pruned_count,
        },
    }
