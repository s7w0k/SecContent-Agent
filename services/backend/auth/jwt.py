"""JWT 访问令牌的签发与验证。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from config import get_settings
from jose import JWTError, jwt


def create_access_token(user_id: str, username: str) -> str:
    """为已认证用户签发访问令牌。"""
    settings = get_settings()
    if not settings.JWT_SECRET:
        raise RuntimeError("JWT_SECRET must be configured before issuing tokens")

    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "username": username,
        "iat": now,
        "exp": now + timedelta(hours=settings.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """验证访问令牌；无效、过期或配置缺失时返回 None。"""
    settings = get_settings()
    if not settings.JWT_SECRET:
        return None
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except (JWTError, ValueError, TypeError):
        return None
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        return None
    return payload
