"""用户风格画像 REST API。"""

from __future__ import annotations

from datetime import datetime

from agent.style_profiler import StyleProfiler
from auth.deps import get_current_user
from fastapi import APIRouter, Depends, HTTPException, Request

router = APIRouter(prefix="/api/profile", tags=["Profile"])


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


def _get_profiler(request: Request) -> StyleProfiler:
    db = _get_db(request)
    profiler = getattr(request.app.state, "style_profiler", None)
    if profiler is not None:
        return profiler
    llm = getattr(request.app.state, "llm", None)
    profiler = StyleProfiler(llm=llm, db=db)
    request.app.state.style_profiler = profiler
    return profiler


def _serialize(document: dict) -> dict:
    result = dict(document)
    result.pop("_id", None)
    for field in ("created_at", "updated_at"):
        if isinstance(result.get(field), datetime):
            result[field] = result[field].isoformat()
    return result


@router.get("/style", summary="获取用户风格画像")
async def get_style_profile(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    profile = await db["user_profiles"].find_one({"user_id": user_id})
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True, "data": _serialize(profile)}


@router.post("/rebuild", summary="重建用户风格画像")
async def rebuild_style_profile(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    profiler = _get_profiler(request)
    profile = await profiler.build_profile(user_id)
    activity_count = await db["user_activities"].count_documents(
        {"user_id": user_id},
    )
    return {
        "ok": True,
        "data": {
            "rebuilt": True,
            "feedback_count": profile["feedback_summary"]["total_feedbacks"],
            "activity_count": activity_count,
            "version": profile["version"],
            "updated_at": profile["updated_at"],
        },
    }
