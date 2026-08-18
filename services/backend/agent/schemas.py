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


_VALID_DOMAINS = {"agent安全", "AI安全", "传统安全", "不相关", "未知"}


class ClassifyResultSchema(BaseModel):
    """Validated output produced by the V2 classification agent."""

    is_relevant: bool | None = Field(
        default=None,
        description="文章核心议题是否直接涉及 AI 安全或智能体安全",
    )
    relevance_confidence: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="相关性判断置信度 0-100",
    )
    relevance_reason: str = Field(default="", max_length=200, description="相关性判断理由")
    category: CategoryName = Field(description="分类类别，必须是6类之一或不相关")
    confidence: int = Field(ge=0, le=100, description="分类置信度 0-100")
    reason: str = Field(max_length=200, description="分类理由")
    is_pr_eligible: bool = Field(default=False, description="是否可进入 PR 流程")
    security_domain: str = Field(
        default="未知",
        description="安全域归属：agent安全 / AI安全 / 传统安全 / 不相关 / 未知",
    )

    @field_validator("security_domain", mode="before")
    @classmethod
    def normalize_security_domain(cls, value: Any) -> str:
        domain = str(value or "").strip()
        return domain if domain in _VALID_DOMAINS else "未知"

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

    @field_validator("relevance_confidence", mode="before")
    @classmethod
    def clamp_relevance_confidence(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            confidence = int(value)
        except (TypeError, ValueError):
            confidence = 50
        return max(0, min(100, confidence))

    @field_validator("reason", "relevance_reason", mode="before")
    @classmethod
    def truncate_reason(cls, value: Any) -> str:
        return str(value or "")[:200]


class SingleProductScoreSchema(BaseModel):
    """单个产品单次 LLM 评分结果。"""

    relevance: int = Field(ge=0, le=100, description="该产品相关性分数 0-100")
    event_impact: int = Field(ge=0, le=100, description="事件影响面 0-100")
    reason: str = Field(default="", max_length=200, description="打分理由")

    @field_validator("relevance", "event_impact", mode="before")
    @classmethod
    def clamp_score(cls, value: Any) -> int:
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 0

    @field_validator("reason", mode="before")
    @classmethod
    def truncate_reason(cls, value: Any) -> str:
        return str(value or "")[:200]
