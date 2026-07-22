"""Per-user prompt configuration models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class UserPromptUpdate(BaseModel):
    """Payload used to save a user-owned prompt override."""

    content: str = Field(..., min_length=50, max_length=20000)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("提示词不能为空")
        return value


class EffectivePrompt(BaseModel):
    """Prompt currently effective for one user, including fallback metadata."""

    prompt_key: str
    content: str
    is_custom: bool
    required_placeholders: list[str]
    updated_at: datetime | None = None
