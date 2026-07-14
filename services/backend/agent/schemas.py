"""Pydantic schemas used by V2 agents for structured LLM output."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CategoryName = Literal[
    "爆点事件",
    "法律法规/监管动态",
    "AI技术重大进展",
    "国内外竞品信息",
    "运营商/行业事件",
    "学术/会展/高校",
    "不相关",
]

_VALID_CATEGORIES = {
    "爆点事件",
    "法律法规/监管动态",
    "AI技术重大进展",
    "国内外竞品信息",
    "运营商/行业事件",
    "学术/会展/高校",
    "不相关",
}


class ClassifyResultSchema(BaseModel):
    """Validated output produced by the V2 classification agent."""

    category: CategoryName = Field(description="分类类别，必须是6类之一或不相关")
    confidence: int = Field(ge=0, le=100, description="分类置信度 0-100")
    reason: str = Field(max_length=200, description="分类理由")
    is_pr_eligible: bool = Field(default=False, description="是否可进入 PR 流程")

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: Any) -> str:
        category = str(value or "").strip()
        return category if category in _VALID_CATEGORIES else "不相关"

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, value: Any) -> int:
        try:
            confidence = int(value)
        except (TypeError, ValueError):
            confidence = 50
        return max(0, min(100, confidence))

    @field_validator("reason", mode="before")
    @classmethod
    def truncate_reason(cls, value: Any) -> str:
        return str(value or "")[:200]


class ScoreResultSchema(BaseModel):
    """Validated output produced by the V2 scoring agent."""

    product_relevance: int = Field(ge=0, le=100, description="产品能力相关度 0-100")
    event_impact: int = Field(ge=0, le=100, description="事件影响面与传播力 0-100")
    reason: str = Field(max_length=200, description="打分理由")
    tags: list[str] = Field(default_factory=list, max_length=5, description="标签列表")

    @field_validator("product_relevance", "event_impact", mode="before")
    @classmethod
    def clamp_score(cls, value: Any) -> int:
        try:
            score = int(value)
        except (TypeError, ValueError):
            score = 0
        return max(0, min(100, score))

    @field_validator("reason", mode="before")
    @classmethod
    def truncate_reason(cls, value: Any) -> str:
        return str(value or "")[:200]

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: Any) -> list[str]:
        tags = value if isinstance(value, list) else [value]
        return [str(tag)[:50] for tag in tags[:5] if tag is not None]
