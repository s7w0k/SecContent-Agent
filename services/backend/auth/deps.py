"""需要登录用户的 FastAPI 依赖。"""

from __future__ import annotations

from fastapi import HTTPException, Request
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
