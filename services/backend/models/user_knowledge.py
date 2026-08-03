"""阶段十五 T0：用户级产品知识库数据模型。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_CONTENT_LENGTH = 100_000
MAX_TERM_LENGTH = 100


class ProductScope(StrEnum):
    """知识条目所关联产品的来源。"""

    GLOBAL = "global"
    USER = "user"


class KnowledgeDocType(StrEnum):
    """知识条目的标准分类。"""

    OVERVIEW = "overview"
    MARKET_BRIEF = "market-brief"
    SALES_BRIEF = "sales-brief"
    CUSTOM = "custom"


def utc_now() -> datetime:
    """返回带时区的 UTC 当前时间。"""
    return datetime.now(UTC)


def compute_content_hash(content: str) -> str:
    """计算 Markdown 内容的稳定 SHA256 哈希。"""
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def new_entry_id() -> str:
    """生成知识条目 UUID。"""
    return str(uuid4())


def new_product_id() -> str:
    """生成用户产品 UUID。"""
    return str(uuid4())


def _required_text(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name}不能为空")
    return cleaned


def _normalize_terms(values: list[str], field_name: str) -> list[str]:
    """清理别名和关键词，并按大小写不敏感去重。"""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            raise ValueError(f"{field_name}不能包含空值")
        if len(value) > MAX_TERM_LENGTH:
            raise ValueError(f"{field_name}单项长度不能超过 {MAX_TERM_LENGTH}")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(value)
    return normalized


class UserKnowledgeEntry(BaseModel):
    """user_knowledge_entries 集合文档。"""

    entry_id: str = Field(default_factory=new_entry_id, min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    product_id: str = Field(min_length=1, max_length=128)
    product_scope: ProductScope
    doc_type: KnowledgeDocType
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    enabled: bool = True
    sort_order: int = Field(default=100, ge=0, le=10_000)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UserProduct(BaseModel):
    """user_products 集合文档。"""

    product_id: str = Field(default_factory=new_product_id, min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    sort_order: int = Field(default=200, ge=0, le=10_000)
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UserKnowledgeEntryCreate(BaseModel):
    """创建知识条目请求。"""

    product_id: str = Field(min_length=1, max_length=128)
    product_scope: ProductScope
    doc_type: KnowledgeDocType
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    enabled: bool = True
    sort_order: int = Field(default=100, ge=0, le=10_000)

    @field_validator("product_id", "title")
    @classmethod
    def clean_required_fields(cls, value: str, info: Any) -> str:
        return _required_text(value, info.field_name)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content不能为空")
        return value


class UserKnowledgeEntryUpdate(BaseModel):
    """部分更新知识条目请求。"""

    product_id: str | None = Field(default=None, min_length=1, max_length=128)
    product_scope: ProductScope | None = None
    doc_type: KnowledgeDocType | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=MAX_CONTENT_LENGTH)
    enabled: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)

    @field_validator("product_id", "title")
    @classmethod
    def clean_required_fields(cls, value: str | None, info: Any) -> str | None:
        return _required_text(value, info.field_name) if value is not None else None

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("content不能为空")
        return value

    @model_validator(mode="after")
    def require_update_field(self) -> UserKnowledgeEntryUpdate:
        if not any(getattr(self, field_name) is not None for field_name in self.model_fields_set):
            raise ValueError("至少提供一个待更新字段")
        return self


class UserProductCreate(BaseModel):
    """注册用户级产品请求。"""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    sort_order: int = Field(default=200, ge=0, le=10_000)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _required_text(value, "name")

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("aliases", "keywords")
    @classmethod
    def normalize_terms(cls, value: list[str], info: Any) -> list[str]:
        return _normalize_terms(value, info.field_name)


class UserProductUpdate(BaseModel):
    """部分更新用户级产品请求。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    aliases: list[str] | None = Field(default=None, max_length=20)
    keywords: list[str] | None = Field(default=None, max_length=50)
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return _required_text(value, "name") if value is not None else None

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("aliases", "keywords")
    @classmethod
    def normalize_terms(cls, value: list[str] | None, info: Any) -> list[str] | None:
        return _normalize_terms(value, info.field_name) if value is not None else None

    @model_validator(mode="after")
    def require_update_field(self) -> UserProductUpdate:
        if not any(getattr(self, field_name) is not None for field_name in self.model_fields_set):
            raise ValueError("至少提供一个待更新字段")
        return self


class UserKnowledgeEntryList(BaseModel):
    """知识条目列表响应数据。"""

    items: list[UserKnowledgeEntry]
    total: int


class ProductCatalogItem(BaseModel):
    """全局产品和用户产品的统一响应数据。"""

    product_id: str
    name: str
    description: str = ""
    scope: ProductScope
    aliases: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    sort_order: int
    enabled: bool
    available_for: list[str] = Field(default_factory=lambda: ["score", "draft", "chat"])


class ProductCatalogList(BaseModel):
    """当前用户可见产品列表响应数据。"""

    items: list[ProductCatalogItem]
    total: int
