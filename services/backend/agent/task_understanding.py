"""Structured task understanding: deterministic facts first, model semantics second."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from agent.contracts.task import SlotSource, TaskAssumption, TaskEnvelope, TaskIntent
from agent.product_catalog import ProductCatalogService
from pydantic import BaseModel, ConfigDict, Field, model_validator

ModelParser = Callable[[str], Awaitable[dict[str, Any] | BaseModel]]


class TaskEnvelopePatch(BaseModel):
    """Only user-editable task slots; identity and authorization are impossible to express."""

    model_config = ConfigDict(extra="forbid")

    intent: TaskIntent | None = None
    goal: str | None = Field(default=None, max_length=4000)
    news_query: str | None = Field(default=None, max_length=1000)
    selected_article_ids: list[str] | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=160)
    product_ids: list[str] | None = Field(default=None, max_length=5)
    template_key: str | None = Field(default=None, max_length=160)
    angle: str | None = Field(default=None, max_length=1000)
    tone: str | None = Field(default=None, max_length=160)
    length: int | None = Field(default=None, ge=100, le=20_000)
    requested_outputs: list[str] | None = Field(default=None, max_length=20)
    save_policy: str | None = Field(default=None, max_length=160)
    constraints: list[str] | None = Field(default=None, max_length=50)
    acceptance_criteria: list[str] | None = Field(default=None, max_length=50)
    risk_level: str | None = Field(default=None, max_length=32)
    draft_artifact: dict[str, Any] | None = None
    draft_version: int | None = Field(default=None, ge=1)
    revision_instruction: str | None = Field(default=None, max_length=4000)
    save_confirmed: bool | None = None
    crawl_approved: bool | None = None
    auto_select: bool | None = None
    explicit_slots: frozenset[str] = Field(default_factory=frozenset)
    confidence: float = Field(default=1.0, ge=0, le=1)
    assumptions: list[TaskAssumption] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_explicit_slots(self) -> TaskEnvelopePatch:
        unknown = self.explicit_slots - set(TaskEnvelope.SLOT_NAMES)
        if unknown:
            raise ValueError(f"unknown explicit slots: {sorted(unknown)}")
        return self

    def slot_values(self) -> dict[str, Any]:
        payload = self.model_dump(exclude_none=True, exclude={"explicit_slots", "confidence", "assumptions"})
        if isinstance(payload.get("intent"), TaskIntent):
            payload["intent"] = payload["intent"].value
        return payload


class TaskUnderstandingResult(BaseModel):
    patch: TaskEnvelopePatch
    parser: str
    warnings: list[str] = Field(default_factory=list)
    model_fallback: bool = False


_INTENT_RULES: tuple[tuple[TaskIntent, tuple[str, ...]], ...] = (
    (TaskIntent.CANCEL, ("取消", "停止任务", "别做了", "cancel")),
    (TaskIntent.ASK_STATUS, ("进度", "状态", "做到哪", "status")),
    (TaskIntent.REVISE, ("修改", "改稿", "重写", "润色", "revise")),
    (TaskIntent.SAVE, ("保存", "存稿", "save")),
    (TaskIntent.SEARCH_AND_RANK, ("搜索", "检索", "找新闻", "排行", "排序", "search")),
    (TaskIntent.GENERATE_DRAFT, ("写稿", "生成稿", "起草", "撰写", "draft")),
)

_ARTICLE_ID = re.compile(
    r"(?:article[_ -]?id|文章(?:编号|ID)?|url[_ -]?hash)\s*[:：=#]?\s*([A-Za-z0-9][A-Za-z0-9_.:-]{5,159})",
    re.IGNORECASE,
)
_LENGTH = re.compile(r"(?:约|控制在|不超过|长度)?\s*(\d{3,5})\s*(?:字|字符)")
_RECENT_DAYS = re.compile(r"(?:最近|近)\s*(\d{1,2})\s*天")


class TaskUnderstandingService:
    def __init__(
        self,
        *,
        model_parser: ModelParser | None = None,
        product_catalog: ProductCatalogService | None = None,
    ):
        self.model_parser = model_parser
        self.catalog = product_catalog or ProductCatalogService()

    async def understand(self, text: str) -> TaskUnderstandingResult:
        text = text.strip()
        if not text:
            raise ValueError("turn content must not be empty")
        deterministic = self._parse_deterministic(text)
        warnings: list[str] = []
        used_model = False
        if self.model_parser is not None:
            try:
                raw = await self.model_parser(text)
                model_patch = TaskEnvelopePatch.model_validate(
                    raw.model_dump() if isinstance(raw, BaseModel) else raw
                )
                deterministic = self._merge_model_patch(deterministic, model_patch)
                used_model = True
            except Exception as exc:
                warnings.append(f"model output rejected; deterministic fallback used: {type(exc).__name__}")
        return TaskUnderstandingResult(
            patch=deterministic,
            parser="deterministic+model" if used_model else "deterministic",
            warnings=warnings,
            model_fallback=bool(self.model_parser and not used_model),
        )

    def _parse_deterministic(self, text: str) -> TaskEnvelopePatch:
        lowered = text.lower()
        selection_only = bool(
            re.fullmatch(
                r"\s*(?:第?[一二三四五六七八九十\d]+(?:个|条|篇)?|最后一个|你决定|你来定|随便|都可以)\s*[。.!！]?\s*",
                text,
            )
        )
        values: dict[str, Any] = {}
        explicit: set[str] = set()
        if not selection_only:
            values["goal"] = text[:4000]
            explicit.add("goal")
        has_search = any(marker in lowered for marker in ("搜索", "检索", "找新闻", "search"))
        has_draft = any(marker in lowered for marker in ("写稿", "生成稿", "起草", "撰写", "draft"))
        if has_search and has_draft:
            values["intent"] = TaskIntent.SEARCH_AND_DRAFT
            explicit.add("intent")
        else:
            for intent, markers in _INTENT_RULES:
                if any(marker.lower() in lowered for marker in markers):
                    values["intent"] = intent
                    explicit.add("intent")
                    break
        article_ids = list(dict.fromkeys(_ARTICLE_ID.findall(text)))
        if article_ids:
            values["selected_article_ids"] = article_ids
            explicit.add("selected_article_ids")

        product_ids: list[str] = []
        for product in self.catalog.list_products(published_only=True):
            names = (product.product_id, product.name, *product.aliases)
            if any(name and name.lower() in lowered for name in names):
                product_ids.append(product.product_id)
        if product_ids:
            values["product_ids"] = list(dict.fromkeys(product_ids))[:5]
            explicit.add("product_ids")

        length_match = _LENGTH.search(text)
        if length_match:
            values["length"] = int(length_match.group(1))
            explicit.add("length")

        days_match = _RECENT_DAYS.search(text)
        if days_match:
            days = min(int(days_match.group(1)), 30)
            values["constraints"] = [
                "published_from=" + (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
            ]
            explicit.add("constraints")

        if values.get("intent") in (TaskIntent.SEARCH_AND_RANK, TaskIntent.SEARCH_AND_DRAFT):
            query = re.sub(r"^(请|帮我|麻烦)?\s*(搜索|检索|找|查)(一下)?", "", text).strip(" ，,。")
            values["news_query"] = (query or text)[:1000]
            explicit.add("news_query")
        if any(marker in text for marker in ("你决定", "你来定", "随便", "都可以")):
            values["assumptions"] = [
                TaskAssumption(text="用户授权系统在未指定的低风险选项中采用默认值", source=SlotSource.USER, confidence=1.0)
            ]
        values["explicit_slots"] = frozenset(explicit)
        return TaskEnvelopePatch(**values)

    @staticmethod
    def _merge_model_patch(
        deterministic: TaskEnvelopePatch, model_patch: TaskEnvelopePatch
    ) -> TaskEnvelopePatch:
        merged = model_patch.model_dump(exclude_none=True)
        # Explicit text facts always win over semantic inference.
        for name, value in deterministic.slot_values().items():
            if name in deterministic.explicit_slots or name not in merged:
                merged[name] = value
        merged["explicit_slots"] = deterministic.explicit_slots
        merged["assumptions"] = [*model_patch.assumptions, *deterministic.assumptions]
        return TaskEnvelopePatch.model_validate(merged)
