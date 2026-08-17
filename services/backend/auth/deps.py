"""需要登录用户的 FastAPI 依赖。"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse


class AuthError(HTTPException):
    """使用统一 API 错误结构的认证异常。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(
            status_code=status_code,
            detail={"code": code, "message": message},
        )


async def auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
    """将认证异常转换为 API 契约规定的错误响应。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": exc.detail},
        headers=exc.headers,
    )


async def get_current_user(request: Request) -> str:
    """从认证中间件写入的 request.state 中取得当前用户 ID。"""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise AuthError(401, "NOT_AUTHENTICATED", "请先登录")
    return user_id


async def get_current_tenant(
    request: Request,
    user_id: str = Depends(get_current_user),
) -> str:
    """Return tenant context established by trusted auth middleware.

    Deployments without a separate tenant claim remain isolated by user ID.
    Client-supplied tenant headers must not define the authorization boundary.
    """
    return str(getattr(request.state, "tenant_id", "") or user_id)


async def get_developer_user(request: Request) -> tuple[str, dict[str, Any]]:
    """返回当前开发者及其用户文档，普通用户不得访问开发者接口。"""

    user_id = await get_current_user(request)
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise AuthError(503, "DATABASE_UNAVAILABLE", "数据库暂不可用")
    user_doc = await db["users"].find_one({"user_id": user_id})
    if not user_doc or not user_doc.get("is_developer", False):
        raise AuthError(403, "FORBIDDEN", "需要开发者权限")
    return user_id, user_doc


async def require_admin(request: Request) -> tuple[str, dict[str, Any]]:
    """返回当前管理员用户及其文档，普通用户不得访问管理员接口。"""

    user_id = await get_current_user(request)
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise AuthError(503, "DATABASE_UNAVAILABLE", "数据库暂不可用")
    user_doc = await db["users"].find_one({"user_id": user_id})
    if not user_doc or not user_doc.get("is_admin", False):
        raise AuthError(403, "FORBIDDEN", "需要管理员权限")
    return user_id, user_doc
