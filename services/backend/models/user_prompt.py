"""Per-user prompt configuration models.

支持版本、乐观锁和向后兼容（draft_system -> draft_generation_business）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class UserPromptUpdate(BaseModel):
    """保存用户提示词覆盖的请求体。"""

    content: str = Field(..., min_length=50, max_length=20000)
    expected_version: int | None = Field(
        None,
        description="乐观锁：当前用户已知版本号，首次保存可不传",
    )

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("提示词不能为空")
        return value


class EffectivePrompt(BaseModel):
    """当前用户生效的提示词，含来源和版本元信息。"""

    prompt_key: str
    content: str
    is_custom: bool
    source: Literal["system", "user"] = "system"
    version: int | None = None
    default_version: int = 1
    required_placeholders: list[str] = []
    allowed_placeholders: list[str] = []
    updated_at: datetime | None = None


class PromptRef(BaseModel):
    """任务快照中引用的提示词版本。"""

    prompt_key: str
    source: Literal["system", "user"]
    version: int
    content_hash: str


class UserPromptVersion(BaseModel):
    """user_prompt_versions 集合的文档模型。"""

    version_id: str
    user_id: str
    prompt_key: str
    version: int
    content: str
    content_hash: str
    base_default_version: int
    change_type: Literal["create", "update", "restore", "reset"]
    created_at: datetime


class UserPromptRecord(BaseModel):
    """user_prompts 集合的文档模型（扩展版本）。"""

    user_id: str
    prompt_key: str
    content: str
    version: int = 1
    base_default_version: int = 1
    enabled: bool = True
    content_hash: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


def compute_content_hash(content: str) -> str:
    """计算提示词内容的 SHA256 哈希。"""
    import hashlib

    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
