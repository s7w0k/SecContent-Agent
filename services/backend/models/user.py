"""用户认证相关的 Pydantic 与 MongoDB 数据模型。"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,20}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def generate_user_id() -> str:
    """生成可读且低碰撞的用户 ID。"""
    date_part = _utc_now().strftime("%Y%m%d")
    return f"u-{date_part}-{secrets.token_hex(3)}"


class UserCreate(BaseModel):
    """注册用户请求。"""

    username: str = Field(min_length=3, max_length=20)
    password: str = Field(min_length=6, max_length=32)
    display_name: str | None = Field(default=None, min_length=1, max_length=50)
    email: str | None = Field(default=None, min_length=3, max_length=254)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        if not USERNAME_PATTERN.fullmatch(value):
            raise ValueError("username must contain only letters, numbers, and underscores")
        return value

    @field_validator("password")
    @classmethod
    def validate_bcrypt_password_length(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("password must not exceed 72 UTF-8 bytes")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is not None and not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("invalid email address")
        return value

    @model_validator(mode="after")
    def default_display_name(self) -> UserCreate:
        if self.display_name is None:
            self.display_name = self.username
        return self


class UserLogin(BaseModel):
    """登录请求。"""

    username: str = Field(min_length=3, max_length=20)
    password: str = Field(min_length=6, max_length=32)


class AccountDelete(BaseModel):
    """注销账号请求；密码确认为可选项。"""

    password: str | None = Field(default=None, min_length=6, max_length=32)


class UserInDB(BaseModel):
    """MongoDB users 集合中的用户文档。"""

    id: str | None = Field(default=None, alias="_id")
    user_id: str = Field(default_factory=generate_user_id)
    username: str = Field(min_length=3, max_length=20)
    email: str | None = Field(default=None, max_length=254)
    hashed_password: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=50)
    is_active: bool = True
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    model_config = {"populate_by_name": True}


class UserPublic(BaseModel):
    """可安全返回给客户端的用户信息。"""

    user_id: str
    username: str
    display_name: str
    email: str | None = None
    created_at: datetime


class TokenResponse(BaseModel):
    """登录成功响应数据。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(gt=0)
    user: UserPublic
