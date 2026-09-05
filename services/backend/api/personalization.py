"""个性化解释反馈 API。

用户可以对单条个性化偏好反馈：有帮助/无影响/不符合/不要再使用。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from agent.memory_event_service import create_memory_event
from agent.personalization_trace import PersonalizationTraceService
from auth.deps import get_current_user
from fastapi import APIRouter, Depends, HTTPException, Request
from models.memory import MemorySourceType
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/personalization", tags=["Personalization"])


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


class PersonalizationFeedbackRequest(BaseModel):
    """个性化解释反馈请求。"""

    generation_id: str
    memory_id: str
    verdict: str = Field(..., pattern=r"^(helpful|no_effect|incorrect|reject)$")
    comment: str | None = None


@router.post("/feedback", summary="个性化解释反馈")
async def submit_personalization_feedback(
    body: PersonalizationFeedbackRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """用户对单条个性化偏好反馈。"""
    db = _get_db(request)

    # 验证 generation_run 存在且属于该用户
    run = await db["generation_runs"].find_one(
        {
            "generation_id": body.generation_id,
            "user_id": user_id,
        }
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Generation run not found")

    # 记录反馈到 GenerationRun
    trace = PersonalizationTraceService(db)
    await trace.record_outcome(
        body.generation_id,
        "personalization_feedback",
        body.verdict,
    )

    # 创建记忆事件
    feedback_id = f"pf-{uuid4().hex[:12]}"
    await create_memory_event(
        db,
        user_id,
        MemorySourceType.PERSONALIZATION_FEEDBACK,
        source_id=feedback_id,
        generation_id=body.generation_id,
        article_url_hash=run.get("article_url_hash"),
        category_v2=run.get("category_v2"),
        payload={
            "memory_id": body.memory_id,
            "verdict": body.verdict,
            "comment": (body.comment or "")[:300],
        },
        idempotency_key=f"pers_feedback:{feedback_id}",
        arq_pool=getattr(request.app.state, "arq_pool", None),
    )

    # 如果用户拒绝，将记忆状态设为 rejected
    if body.verdict == "reject":
        await db["user_memory_items"].update_one(
            {"memory_id": body.memory_id, "user_id": user_id},
            {
                "$set": {
                    "status": "rejected",
                    "updated_at": datetime.now(UTC),
                }
            },
        )

    return {"ok": True, "data": {"feedback_id": feedback_id}}
