"""Web search Pydantic models - request/response schemas for search API."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SearchCategory(str):
    """Search category constants."""
    GENERAL = "general"
    NEWS = "news"


class SearchTimeRange(StrEnum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


class SearchWarning(BaseModel):
    """Warning for partial search results."""
    code: str
    message: str
    count: int = 0


class WebSearchRequest(BaseModel):
    """Search query request."""
    q: str = Field(..., min_length=2, max_length=200, description="搜索关键词")
    categories: list[str] = Field(default=["general"], description="搜索分类")
    language: str = Field(default="all", description="搜索语言")
    time_range: str | None = Field(default=None, description="时间范围: day/month/year")
    safesearch: int = Field(default=1, ge=0, le=2, description="安全搜索级别")
    pageno: int = Field(default=1, ge=1, le=10, description="页码")

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, v: list[str]) -> list[str]:
        if not v:
            return ["general"]
        if len(v) > 2:
            raise ValueError("最多选择 2 个分类")
        allowed = {"general", "news"}
        for c in v:
            if c not in allowed:
                raise ValueError(f"不允许的分类: {c}")
        return v

    @field_validator("time_range")
    @classmethod
    def validate_time_range(cls, v: str | None) -> str | None:
        if v is None:
            return None
        allowed = {"day", "month", "year"}
        if v not in allowed:
            raise ValueError(f"不允许的时间范围: {v}")
        return v


class WebSearchResult(BaseModel):
    """Normalized search result item."""
    result_id: str
    title: str = Field(..., max_length=500)
    url: str
    display_domain: str
    snippet: str = Field(default="", max_length=2000)
    published_at: str | None = None
    engines: list[str] = Field(default_factory=list)
    category: str = "general"
    searxng_score: float | None = None
    is_imported: bool = False
    article_url_hash: str | None = None


class WebSearchResponse(BaseModel):
    """Search query response."""
    search_id: str
    query: dict[str, Any]
    results: list[WebSearchResult]
    page: int
    has_more: bool
    warnings: list[SearchWarning] = Field(default_factory=list)
    expires_at: str


class SearchSessionResponse(BaseModel):
    """Get session response (excludes internal fields)."""
    search_id: str
    query: dict[str, Any]
    results: list[WebSearchResult]
    page: int
    has_more: bool
    warnings: list[SearchWarning] = Field(default_factory=list)
    expires_at: str


class SearchImportRequest(BaseModel):
    """Import selected results request."""
    search_id: str = Field(..., min_length=1)
    result_ids: list[str] = Field(..., min_length=1, max_length=20)


class SearchImportItemStatus(StrEnum):
    IMPORTED = "imported"
    DUPLICATE = "duplicate"
    INVALID_URL = "invalid_url"
    FAILED = "failed"


class SearchImportItem(BaseModel):
    """Single import result item."""
    result_id: str
    status: SearchImportItemStatus
    article_url_hash: str | None = None
    message: str = ""


class SearchImportSummary(BaseModel):
    """Import batch summary."""
    requested: int
    imported: int = 0
    duplicate: int = 0
    failed: int = 0
    enrichment_queued: int = 0


class SearchImportResponse(BaseModel):
    """Import batch response."""
    batch_id: str
    summary: SearchImportSummary
    items: list[SearchImportItem]


class SearchStatusResponse(BaseModel):
    """Search feature status."""
    enabled: bool
    available: bool
    allowed_categories: list[str]
    allowed_languages: list[str]
    max_import_items: int


class ImportBatchStatus(StrEnum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
