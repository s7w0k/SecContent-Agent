"""用户显式偏好 Policy REST API。

提供 GET/PUT/reset 接口，支持乐观锁并发控制。
自动学习记忆不能覆盖显式 Policy（约束 C1）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from auth.deps import get_current_user
from config import get_settings
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/profile-policy", tags=["ProfilePolicy"])


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


def _default_policy(user_id: str) -> dict:
    return {
        "policy_id": f"policy-{uuid4().hex[:8]}",
        "user_id": user_id,
        "content_focus": [],
        "opening_style": None,
        "structure_preference": None,
        "required_patterns": [],
        "avoid_patterns": [],
        "custom_instructions": None,
        "auto_learning_enabled": True,
        "memory_write_approval": True,
        "version": 1,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }


class PolicyUpdate(BaseModel):
    """Policy 更新请求体 - PR 稿内容生成偏好。"""

    content_focus: list[str] = Field(default_factory=list, max_length=10)
    opening_style: str | None = None
    structure_preference: str | None = None
    required_patterns: list[str] = Field(default_factory=list, max_length=20)
    avoid_patterns: list[str] = Field(default_factory=list, max_length=20)
    custom_instructions: str | None = Field(default=None, max_length=2000)
    auto_learning_enabled: bool = True
    memory_write_approval: bool = True


@router.get("", summary="获取用户显式偏好 Policy")
async def get_policy(request: Request, user_id: str = Depends(get_current_user)):
    db = _get_db(request)
    doc = await db["user_profile_policies"].find_one({"user_id": user_id})
    if doc is None:
        return {"ok": True, "data": {"policy": _default_policy(user_id), "is_default": True, "version": 1}}
    doc.pop("_id", None)
    return {"ok": True, "data": {"policy": doc, "is_default": False, "version": doc.get("version", 1)}}


@router.put("", summary="保存用户显式偏好 Policy")
async def save_policy(
    body: PolicyUpdate,
    request: Request,
    user_id: str = Depends(get_current_user),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    db = _get_db(request)
    existing = await db["user_profile_policies"].find_one({"user_id": user_id})
    current_version = existing.get("version", 1) if existing else 1

    # 乐观锁检查
    if if_match is not None:
        try:
            expected_version = int(if_match)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid If-Match header")
        if expected_version != current_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "POLICY_VERSION_CONFLICT",
                    "message": "Policy was modified by another request",
                    "current_version": current_version,
                },
            )

    new_version = current_version + 1
    now = datetime.now(UTC)

    update_doc = {
        "content_focus": body.content_focus,
        "opening_style": body.opening_style,
        "structure_preference": body.structure_preference,
        "required_patterns": [p[:200] for p in body.required_patterns],
        "avoid_patterns": [p[:200] for p in body.avoid_patterns],
        "custom_instructions": (body.custom_instructions or "")[:2000] if body.custom_instructions else None,
        "auto_learning_enabled": body.auto_learning_enabled,
        "memory_write_approval": body.memory_write_approval,
        "version": new_version,
        "updated_at": now,
    }

    if existing is None:
        update_doc["policy_id"] = f"policy-{uuid4().hex[:8]}"
        update_doc["user_id"] = user_id
        update_doc["created_at"] = now
        await db["user_profile_policies"].insert_one(update_doc)
    else:
        await db["user_profile_policies"].update_one(
            {"user_id": user_id, "version": current_version},
            {"$set": update_doc},
        )

    update_doc.pop("_id", None)
    return {"ok": True, "data": {"policy": update_doc, "version": new_version}}


@router.post("/reset", summary="重置用户显式偏好 Policy")
async def reset_policy(request: Request, user_id: str = Depends(get_current_user)):
    """只删除显式 Policy，不删除自动记忆。"""
    db = _get_db(request)
    await db["user_profile_policies"].delete_one({"user_id": user_id})
    return {"ok": True, "data": {"policy": _default_policy(user_id), "is_default": True, "version": 1}}
