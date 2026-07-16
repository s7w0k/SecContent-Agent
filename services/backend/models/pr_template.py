"""多租户用户自定义 PR 模板数据模型。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TEMPLATE_SERIALIZED_LENGTH = 20_000


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _template_id() -> str:
    return f"tpl-{uuid4().hex}"


def _version_id() -> str:
    return f"tplv-{uuid4().hex}"


class TemplateKey(StrEnum):
    """六套系统 PR 模板的跨版本稳定键。"""

    BREAKING_A = "breaking_a"
    BREAKING_B = "breaking_b"
    LAW_A = "law_a"
    LAW_B = "law_b"
    AI_A = "ai_a"
    AI_B = "ai_b"


class PRTemplateCategory(StrEnum):
    """当前允许进入 PR 草稿流程的 V2 分类。"""

    BREAKING_EVENT = "爆点事件"
    LAW_AND_REGULATION = "法律法规/监管动态"
    AI_TECH_PROGRESS = "AI技术重大进展"


class TemplateSlot(StrEnum):
    """同一 PR 分类下的固定模板槽位。"""

    A = "A"
    B = "B"


class TemplateSource(StrEnum):
    """有效模板的来源。"""

    SYSTEM = "system"
    USER = "user"


class TemplateChangeType(StrEnum):
    """模板历史版本产生方式。"""

    CREATE = "create"
    UPDATE = "update"
    RESET = "reset"
    RESTORE = "restore"


TEMPLATE_IDENTITY: dict[TemplateKey, tuple[PRTemplateCategory, TemplateSlot]] = {
    TemplateKey.BREAKING_A: (PRTemplateCategory.BREAKING_EVENT, TemplateSlot.A),
    TemplateKey.BREAKING_B: (PRTemplateCategory.BREAKING_EVENT, TemplateSlot.B),
    TemplateKey.LAW_A: (PRTemplateCategory.LAW_AND_REGULATION, TemplateSlot.A),
    TemplateKey.LAW_B: (PRTemplateCategory.LAW_AND_REGULATION, TemplateSlot.B),
    TemplateKey.AI_A: (PRTemplateCategory.AI_TECH_PROGRESS, TemplateSlot.A),
    TemplateKey.AI_B: (PRTemplateCategory.AI_TECH_PROGRESS, TemplateSlot.B),
}


class TemplateSection(BaseModel):
    """用户可编辑的模板章节。"""

    heading: str = Field(min_length=1, max_length=100)
    guide: str = Field(min_length=1, max_length=1000)
    order: int = Field(ge=1, le=12)

    model_config = {"extra": "forbid"}

    @field_validator("heading", "guide", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> str:
        return str(value or "").strip()


class TemplateContent(BaseModel):
    """模板中允许用户编辑的结构化内容。"""

    name: str = Field(min_length=1, max_length=100)
    title_template: str = Field(min_length=1, max_length=300)
    sections: list[TemplateSection] = Field(min_length=1, max_length=12)
    perspectives: list[str] = Field(min_length=2, max_length=2)
    extra_instructions: str = Field(default="", max_length=2000)

    model_config = {"extra": "forbid"}

    @field_validator("name", "title_template", "extra_instructions", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("perspectives", mode="before")
    @classmethod
    def normalize_perspectives(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [str(item or "").strip() for item in value]

    @model_validator(mode="after")
    def validate_structure(self) -> Self:
        if any(not perspective for perspective in self.perspectives):
            raise ValueError("perspectives cannot contain blank values")
        if len({item.casefold() for item in self.perspectives}) != len(self.perspectives):
            raise ValueError("perspectives must be distinct")

        headings = [section.heading.casefold() for section in self.sections]
        if len(set(headings)) != len(headings):
            raise ValueError("section headings must be distinct")

        self.sections.sort(key=lambda section: section.order)
        for index, section in enumerate(self.sections, start=1):
            section.order = index

        serialized = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        if len(serialized) > MAX_TEMPLATE_SERIALIZED_LENGTH:
            raise ValueError(
                f"template content exceeds {MAX_TEMPLATE_SERIALIZED_LENGTH} characters"
            )
        return self


class UserPRTemplateUpdate(TemplateContent):
    """保存用户模板覆盖时的请求模型。"""

    expected_version: int | None = Field(default=None, ge=1)


class TemplateSnapshot(TemplateContent):
    """草稿或历史版本中保存的不可变模板内容快照。"""

    template_key: TemplateKey
    category_v2: PRTemplateCategory
    slot: TemplateSlot

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        expected = TEMPLATE_IDENTITY[self.template_key]
        if (self.category_v2, self.slot) != expected:
            raise ValueError("template_key does not match category_v2 and slot")
        return self


class UserPRTemplate(TemplateContent):
    """MongoDB 中当前生效的用户模板覆盖文档。"""

    id: str | None = Field(default=None, alias="_id")
    template_id: str = Field(default_factory=_template_id, min_length=1, max_length=100)
    user_id: str = Field(min_length=1, max_length=100)
    template_key: TemplateKey
    base_system_version: int = Field(default=1, ge=1)
    category_v2: PRTemplateCategory
    slot: TemplateSlot
    version: int = Field(default=1, ge=1)
    enabled: bool = True
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        expected = TEMPLATE_IDENTITY[self.template_key]
        if (self.category_v2, self.slot) != expected:
            raise ValueError("template_key does not match category_v2 and slot")
        return self


class EffectivePRTemplate(TemplateSnapshot):
    """系统默认与用户覆盖合并后的模板响应。"""

    template_id: str = Field(min_length=1, max_length=100)
    source: TemplateSource
    version: int = Field(ge=1)
    system_version: int = Field(ge=1)
    updated_at: datetime | None = None


class UserPRTemplateVersion(BaseModel):
    """MongoDB 中的用户模板历史版本文档。"""

    id: str | None = Field(default=None, alias="_id")
    version_id: str = Field(default_factory=_version_id, min_length=1, max_length=100)
    template_id: str = Field(min_length=1, max_length=100)
    user_id: str = Field(min_length=1, max_length=100)
    template_key: TemplateKey
    version: int = Field(ge=1)
    snapshot: TemplateSnapshot
    change_type: TemplateChangeType
    created_at: datetime = Field(default_factory=_utc_now)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> Self:
        if self.snapshot.template_key != self.template_key:
            raise ValueError("version template_key does not match snapshot")
        return self
