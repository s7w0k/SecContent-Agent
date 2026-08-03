"""用户生成偏好 API - GET/PUT/reset。

用户可以保存账号级默认的产品相关性开关、产品选择模式和默认产品。
单次生成请求的 generation_options 优先于账号级偏好。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from agent.product_catalog import ProductCatalogService
from auth.deps import AuthError, get_current_user
from config import get_settings
from fastapi import APIRouter, Depends, Request
from models.generation_config import (
    ProductTargetMode,
    UserGenerationPreferences,
    UserGenerationPreferencesUpdate,
)

logger = logging.getLogger("backend.api.generation_preferences")

router = APIRouter(prefix="/api/generation-preferences", tags=["Generation Preferences"])

# 系统默认阈值
DEFAULT_PRODUCT_EVENT_THRESHOLD = 80
DEFAULT_EVENT_ONLY_THRESHOLD = 60


class PreferenceError(AuthError):
    """Preference error."""


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise PreferenceError(503, "DATABASE_UNAVAILABLE", "数据库暂不可用")
    return db


async def _get_preferences(db, user_id: str) -> UserGenerationPreferences:
    """获取用户偏好，不存在返回系统默认。"""
    doc = await db["user_generation_preferences"].find_one({"user_id": user_id})
    if doc is None:
        return UserGenerationPreferences(
            user_id=user_id,
            product_relevance_enabled=True,
            product_target_mode=ProductTargetMode.AUTO,
            selected_product_ids=[],
            product_event_threshold=DEFAULT_PRODUCT_EVENT_THRESHOLD,
            event_only_threshold=DEFAULT_EVENT_ONLY_THRESHOLD,
            version=1,
        )

    doc.pop("_id", None)
    return UserGenerationPreferences(**doc)


def _validate_product_ids(product_ids: list[str], mode: ProductTargetMode) -> None:
    """校验产品 ID。"""
    if mode == ProductTargetMode.SELECTED:
        if not product_ids:
            raise PreferenceError(422, "INVALID_PRODUCT_SELECTION", "selected 模式必须指定至少一个产品")
        catalog = ProductCatalogService(get_settings().KNOWLEDGE_BASE_DIR)
        try:
            catalog.validate_product_ids(product_ids, max_count=5)
        except ValueError as exc:
            raise PreferenceError(422, "PRODUCT_UNAVAILABLE", str(exc)) from None


@router.get("", summary="获取当前用户的生成偏好")
async def get_preferences(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """返回账号级默认生成偏好。"""
    prefs = await _get_preferences(_get_db(request), user_id)
    return {"ok": True, "data": prefs.model_dump()}


@router.put("", summary="保存生成偏好")
async def save_preferences(
    body: UserGenerationPreferencesUpdate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """保存账号级默认生成偏好，支持乐观锁。"""
    db = _get_db(request)

    # 校验 mode 和产品 ID
    _validate_product_ids(body.selected_product_ids, body.product_target_mode)

    # none 自动关闭产品相关性
    relevance = body.product_relevance_enabled
    if body.product_target_mode == ProductTargetMode.NONE:
        relevance = False

    existing = await db["user_generation_preferences"].find_one({"user_id": user_id})
    if existing is None:
        new_version = 1
    else:
        current_version = existing.get("version", 1)
        if body.expected_version is not None and body.expected_version != current_version:
            raise PreferenceError(
                409,
                "PREFERENCE_VERSION_CONFLICT",
                f"版本冲突：期望 {body.expected_version}，实际 {current_version}",
            ) from None
        new_version = current_version + 1

    now = datetime.now(UTC)
    update_doc = {
        "user_id": user_id,
        "product_relevance_enabled": relevance,
        "product_target_mode": body.product_target_mode.value,
        "selected_product_ids": body.selected_product_ids,
        "product_event_threshold": DEFAULT_PRODUCT_EVENT_THRESHOLD,
        "event_only_threshold": DEFAULT_EVENT_ONLY_THRESHOLD,
        "version": new_version,
        "updated_at": now,
    }

    await db["user_generation_preferences"].update_one(
        {"user_id": user_id},
        {
            "$set": update_doc,
            "$setOnInsert": {"created_at": now, "preference_id": f"pref-{uuid4()}"},
        },
        upsert=True,
    )

    logger.info(
        "Preferences saved: user=%s mode=%s relevance=%s version=%d",
        user_id,
        body.product_target_mode.value,
        relevance,
        new_version,
    )

    return {
        "ok": True,
        "data": UserGenerationPreferences(
            user_id=user_id,
            product_relevance_enabled=relevance,
            product_target_mode=body.product_target_mode,
            selected_product_ids=body.selected_product_ids,
            product_event_threshold=DEFAULT_PRODUCT_EVENT_THRESHOLD,
            event_only_threshold=DEFAULT_EVENT_ONLY_THRESHOLD,
            version=new_version,
            updated_at=now,
        ).model_dump(),
    }


@router.post("/reset", summary="恢复系统默认")
async def reset_preferences(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """恢复系统默认生成偏好。"""
    db = _get_db(request)
    await db["user_generation_preferences"].delete_one({"user_id": user_id})

    logger.info("Preferences reset: user=%s", user_id)

    prefs = await _get_preferences(db, user_id)
    return {"ok": True, "data": prefs.model_dump()}
