"""Schema and privacy/coverage validation for full-loop journey datasets."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from agent.contracts.task import SlotSource, SlotState, TaskEnvelope
from pydantic import BaseModel, ConfigDict, Field, model_validator

DATASET_PATH = Path(__file__).with_name("dataset.v1.jsonl")
REQUIRED_CATEGORIES = frozenset(
    {
        "explicit_article",
        "vague_news",
        "multiple_candidates",
        "no_candidates",
        "missing_product",
        "product_conflict",
        "category_conflict",
        "force_low_score",
        "revise_and_save",
        "midcourse_change",
        "recovery",
        "tool_failure",
        "security",
    }
)
KNOWN_TOOLS = frozenset(
    {
        "search_news",
        "crawl_news",
        "list_articles",
        "get_article",
        "select_article_candidates",
        "classify_article",
        "match_products",
        "score_article",
        "generate_draft",
        "review_draft",
        "revise_draft",
        "save_draft_version",
        "export_draft",
        "publish_draft",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{12,}"),
    re.compile(r"bearer\s+[A-Za-z0-9._-]{12,}", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|password|secret)\s*[:=]\s*\S+", re.IGNORECASE),
)


class JourneyTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(..., min_length=1, max_length=100)
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1, max_length=4000)


class JourneyCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., pattern=r"^FLJ-\d{3}$")
    dataset_version: str = Field(..., pattern=r"^1\.0$")
    category: str
    user_turns: list[JourneyTurn] = Field(..., min_length=1, max_length=10)
    initial_state: dict[str, Any]
    expected_slots: dict[str, Any] = Field(..., min_length=2)
    expected_slot_sources: dict[str, SlotSource] = Field(default_factory=dict)
    allowed_tools: list[str]
    forbidden_tools: list[str]
    expected_questions: list[str]
    acceptance_criteria: list[str] = Field(..., min_length=1)
    acceptable_terminal_statuses: list[str] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_case(self) -> JourneyCase:
        if self.category not in REQUIRED_CATEGORIES:
            raise ValueError(f"unknown journey category: {self.category}")
        unknown = (set(self.allowed_tools) | set(self.forbidden_tools)) - KNOWN_TOOLS
        if unknown:
            raise ValueError(f"unknown tools: {', '.join(sorted(unknown))}")
        overlap = set(self.allowed_tools) & set(self.forbidden_tools)
        if overlap:
            raise ValueError(f"tools cannot be both allowed and forbidden: {sorted(overlap)}")
        unknown_slots = set(self.expected_slots) - set(TaskEnvelope.SLOT_NAMES)
        if unknown_slots:
            raise ValueError(f"unknown expected slots: {sorted(unknown_slots)}")
        if set(self.expected_slot_sources) - set(self.expected_slots):
            raise ValueError("expected_slot_sources must reference expected_slots")
        return self

    def to_task_envelope(self) -> TaskEnvelope:
        fields: dict[str, Any] = {
            "task_id": f"task-{self.case_id.lower()}",
            "thread_id": f"thread-{self.case_id.lower()}",
            "user_id": "eval-user",
            "tenant_id": "eval-tenant",
        }
        for name, value in self.expected_slots.items():
            source = self.expected_slot_sources.get(name, SlotSource.USER)
            fields[name] = SlotState.from_value(
                value,
                source=source,
                confidence=1.0 if source == SlotSource.USER else 0.8,
                confirmed=source == SlotSource.USER,
            )
        return TaskEnvelope.model_validate(fields)


def load_dataset(path: Path = DATASET_PATH) -> list[JourneyCase]:
    cases: list[JourneyCase] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                cases.append(JourneyCase.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"invalid journey at line {line_number}: {exc}") from exc
    return cases


def validate_dataset(cases: list[JourneyCase]) -> dict[str, Any]:
    if len(cases) < 60:
        raise ValueError(f"journey dataset requires at least 60 cases, got {len(cases)}")
    ids = [case.case_id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("journey case_id values must be unique")
    counts = Counter(case.category for case in cases)
    underfilled = {name: counts[name] for name in REQUIRED_CATEGORIES if counts[name] < 5}
    if underfilled:
        raise ValueError(f"each journey category requires at least 5 cases: {underfilled}")
    serialized = "\n".join(case.model_dump_json() for case in cases)
    for pattern in _SECRET_PATTERNS:
        if pattern.search(serialized):
            raise ValueError(f"dataset contains credential-like content: {pattern.pattern}")
    for case in cases:
        case.to_task_envelope()
    return {"total": len(cases), "categories": dict(sorted(counts.items()))}
