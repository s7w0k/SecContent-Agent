"""Conversation task and slot contracts for the full-loop Agent.

Identity fields are server-owned. Model output may only patch named slots through
``TaskEnvelope.apply_model_patch``; attempts to replace task/user/tenant identity
are rejected instead of being silently accepted.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"
MAX_SLOT_TEXT = 4000
MAX_SLOT_ITEMS = 100


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TaskIntent(StrEnum):
    UNKNOWN = "unknown"
    GENERATE_DRAFT = "generate_draft"
    SEARCH_AND_DRAFT = "search_and_draft"
    CURATE_NEWS = "curate_news"
    REVISE_DRAFT = "revise_draft"
    REVIEW_DRAFT = "review_draft"
    SAVE_DRAFT = "save_draft"
    EXPORT_DRAFT = "export_draft"
    # Conversational API aliases. Legacy intents remain accepted for rollback.
    SEARCH_AND_RANK = "search_and_rank"
    REVISE = "revise"
    SAVE = "save"
    ASK_STATUS = "ask_status"
    CANCEL = "cancel"


class SlotStatus(StrEnum):
    UNKNOWN = "unknown"
    INFERRED = "inferred"
    CONFIRMED = "confirmed"
    CONFLICTED = "conflicted"
    NOT_APPLICABLE = "not_applicable"


class SlotSource(StrEnum):
    USER = "user"
    TOOL = "tool"
    MEMORY = "memory"
    DEFAULT = "default"
    MODEL = "model"


class SavePolicy(StrEnum):
    NO_SAVE = "no_save"
    SAVE_VERSION = "save_version"
    REQUIRE_CONFIRMATION = "require_confirmation"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskIdentityOverrideError(ValueError):
    """A model patch attempted to replace a server-owned identity field."""


class SlotConflict(BaseModel):
    """An alternative value retained for clarification and audit."""

    value: Any = None
    source: SlotSource
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    turn_id: str = Field(default="", max_length=100)


def _validate_slot_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_SLOT_TEXT:
        raise ValueError(f"slot text exceeds {MAX_SLOT_TEXT} characters")
    if isinstance(value, (list, tuple, set)):
        if len(value) > MAX_SLOT_ITEMS:
            raise ValueError(f"slot list exceeds {MAX_SLOT_ITEMS} items")
        for item in value:
            _validate_slot_value(item)
    if isinstance(value, dict):
        if len(value) > MAX_SLOT_ITEMS:
            raise ValueError(f"slot object exceeds {MAX_SLOT_ITEMS} entries")
        for key, item in value.items():
            _validate_slot_value(key)
            _validate_slot_value(item)
    return value


class SlotState(BaseModel):
    """Value plus provenance, confidence and conflict state for one slot."""

    model_config = ConfigDict(extra="forbid")

    value: Any = None
    status: SlotStatus = SlotStatus.UNKNOWN
    source: SlotSource = SlotSource.DEFAULT
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    updated_turn_id: str = Field(default="", max_length=100)
    updated_at: datetime = Field(default_factory=_utc_now)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    conflicts: list[SlotConflict] = Field(default_factory=list, max_length=20)

    @field_validator("value")
    @classmethod
    def validate_value_size(cls, value: Any) -> Any:
        return _validate_slot_value(value)

    @model_validator(mode="after")
    def validate_state_value(self) -> SlotState:
        if (
            self.status in {SlotStatus.UNKNOWN, SlotStatus.NOT_APPLICABLE}
            and self.value is not None
        ):
            raise ValueError(f"{self.status.value} slot must not contain a value")
        if (
            self.status in {SlotStatus.INFERRED, SlotStatus.CONFIRMED, SlotStatus.CONFLICTED}
            and self.value is None
        ):
            raise ValueError(f"{self.status.value} slot requires a value")
        if self.status == SlotStatus.CONFIRMED and self.source != SlotSource.USER:
            raise ValueError("confirmed slot must have user source")
        return self

    @classmethod
    def unknown(cls) -> SlotState:
        return cls()

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        source: SlotSource,
        confidence: float = 1.0,
        turn_id: str = "",
        confirmed: bool | None = None,
        evidence_refs: list[str] | None = None,
    ) -> SlotState:
        is_confirmed = source == SlotSource.USER if confirmed is None else confirmed
        status = SlotStatus.CONFIRMED if is_confirmed else SlotStatus.INFERRED
        return cls(
            value=value,
            status=status,
            source=source,
            confidence=confidence,
            updated_turn_id=turn_id,
            evidence_refs=list(evidence_refs or []),
        )


_SOURCE_PRIORITY = {
    SlotSource.DEFAULT: 1,
    SlotSource.MODEL: 2,
    SlotSource.MEMORY: 3,
    SlotSource.TOOL: 4,
    SlotSource.USER: 5,
}


def _append_conflict(current: SlotState, incoming: SlotState) -> SlotState:
    conflict = SlotConflict(
        value=incoming.value,
        source=incoming.source,
        confidence=incoming.confidence,
        turn_id=incoming.updated_turn_id,
    )
    conflicts = [*current.conflicts, conflict]
    return current.model_copy(
        update={"status": SlotStatus.CONFLICTED, "conflicts": conflicts[-20:]}
    )


def merge_slot(current: SlotState, incoming: SlotState) -> SlotState:
    """Merge one slot using provenance and recency rules.

    The latest explicit user value wins. A confirmed user value cannot be
    overwritten by tool, memory, model or default data; disagreement is retained
    as a conflict so the clarification policy can surface it.
    """
    if incoming.status == SlotStatus.UNKNOWN:
        return current
    if incoming.status == SlotStatus.NOT_APPLICABLE:
        return incoming if incoming.source == SlotSource.USER else current
    if current.status in {SlotStatus.UNKNOWN, SlotStatus.NOT_APPLICABLE}:
        return incoming
    if current.value == incoming.value:
        if incoming.source == SlotSource.USER:
            return incoming.model_copy(update={"status": SlotStatus.CONFIRMED, "conflicts": []})
        if _SOURCE_PRIORITY[incoming.source] > _SOURCE_PRIORITY[current.source]:
            return incoming.model_copy(update={"conflicts": current.conflicts})
        return current
    if incoming.source == SlotSource.USER:
        return incoming.model_copy(update={"status": SlotStatus.CONFIRMED, "conflicts": []})
    if current.status == SlotStatus.CONFIRMED and current.source == SlotSource.USER:
        return _append_conflict(current, incoming)
    if _SOURCE_PRIORITY[incoming.source] > _SOURCE_PRIORITY[current.source]:
        return incoming
    return _append_conflict(current, incoming)


class TaskAssumption(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    source: SlotSource = SlotSource.MODEL
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ConversationTurn(BaseModel):
    """Persisted user-visible turn. Private reasoning is never a valid role."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(..., min_length=1, max_length=100)
    sequence: int = Field(..., ge=1)
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1, max_length=12000)
    content_hash: str = ""
    created_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def set_content_hash(self) -> ConversationTurn:
        if self.content_hash:
            return self
        digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        self.content_hash = f"sha256:{digest}"
        return self


class TaskEnvelope(BaseModel):
    """Versioned, forward-compatible conversation task contract."""

    model_config = ConfigDict(extra="allow")

    SLOT_NAMES: ClassVar[tuple[str, ...]] = (
        "intent",
        "goal",
        "news_query",
        "selected_article_ids",
        "category",
        "product_ids",
        "template_key",
        "angle",
        "tone",
        "length",
        "requested_outputs",
        "save_policy",
        "constraints",
        "acceptance_criteria",
        "risk_level",
        "draft_artifact",
        "draft_version",
        "revision_instruction",
        "save_confirmed",
        "crawl_approved",
        "auto_select",
    )
    PROTECTED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"task_id", "thread_id", "user_id", "tenant_id", "schema_version"}
    )

    schema_version: str = SCHEMA_VERSION
    task_id: str = Field(..., min_length=1, max_length=100)
    thread_id: str = Field(..., min_length=1, max_length=100)
    user_id: str = Field(..., min_length=1, max_length=100)
    tenant_id: str = Field(default="", max_length=100)

    intent: SlotState = Field(default_factory=SlotState.unknown)
    goal: SlotState = Field(default_factory=SlotState.unknown)
    news_query: SlotState = Field(default_factory=SlotState.unknown)
    selected_article_ids: SlotState = Field(default_factory=SlotState.unknown)
    category: SlotState = Field(default_factory=SlotState.unknown)
    product_ids: SlotState = Field(default_factory=SlotState.unknown)
    template_key: SlotState = Field(default_factory=SlotState.unknown)
    angle: SlotState = Field(default_factory=SlotState.unknown)
    tone: SlotState = Field(default_factory=SlotState.unknown)
    length: SlotState = Field(default_factory=SlotState.unknown)
    requested_outputs: SlotState = Field(default_factory=SlotState.unknown)
    save_policy: SlotState = Field(default_factory=SlotState.unknown)
    constraints: SlotState = Field(default_factory=SlotState.unknown)
    acceptance_criteria: SlotState = Field(default_factory=SlotState.unknown)
    risk_level: SlotState = Field(default_factory=SlotState.unknown)
    draft_artifact: SlotState = Field(default_factory=SlotState.unknown)
    draft_version: SlotState = Field(default_factory=SlotState.unknown)
    revision_instruction: SlotState = Field(default_factory=SlotState.unknown)
    save_confirmed: SlotState = Field(default_factory=SlotState.unknown)
    crawl_approved: SlotState = Field(default_factory=SlotState.unknown)
    auto_select: SlotState = Field(default_factory=SlotState.unknown)

    missing_slots: list[str] = Field(default_factory=list, max_length=30)
    ambiguous_slots: list[str] = Field(default_factory=list, max_length=30)
    assumptions: list[TaskAssumption] = Field(default_factory=list, max_length=30)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if not value.startswith("1."):
            raise ValueError(f"unsupported task envelope schema_version: {value!r}")
        return value

    @model_validator(mode="after")
    def validate_known_enums(self) -> TaskEnvelope:
        enum_slots: dict[str, type[StrEnum]] = {
            "intent": TaskIntent,
            "save_policy": SavePolicy,
            "risk_level": RiskLevel,
        }
        for name, enum_type in enum_slots.items():
            slot = getattr(self, name)
            if slot.value is not None:
                enum_type(slot.value)
        return self

    @classmethod
    def from_user_input(
        cls,
        *,
        task_id: str,
        thread_id: str,
        user_id: str,
        goal: str,
        tenant_id: str = "",
        intent: TaskIntent = TaskIntent.UNKNOWN,
        acceptance_criteria: list[str] | None = None,
        turn_id: str = "",
    ) -> TaskEnvelope:
        return cls(
            task_id=task_id,
            thread_id=thread_id,
            user_id=user_id,
            tenant_id=tenant_id,
            intent=SlotState.from_value(intent.value, source=SlotSource.USER, turn_id=turn_id),
            goal=SlotState.from_value(goal, source=SlotSource.USER, turn_id=turn_id),
            acceptance_criteria=SlotState.from_value(
                list(acceptance_criteria or []), source=SlotSource.USER, turn_id=turn_id
            ),
        )

    def slot_states(self) -> dict[str, SlotState]:
        return {name: getattr(self, name) for name in self.SLOT_NAMES}

    def apply_model_patch(self, patch: dict[str, Any], *, turn_id: str = "") -> TaskEnvelope:
        protected = self.PROTECTED_FIELDS.intersection(patch)
        if protected:
            names = ", ".join(sorted(protected))
            raise TaskIdentityOverrideError(f"model patch cannot override server fields: {names}")
        updates: dict[str, Any] = {}
        for name, raw in patch.items():
            if name not in self.SLOT_NAMES:
                continue
            incoming = (
                SlotState.model_validate(raw)
                if isinstance(raw, dict) and "status" in raw
                else SlotState.from_value(
                    raw,
                    source=SlotSource.MODEL,
                    confidence=0.5,
                    turn_id=turn_id,
                    confirmed=False,
                )
            )
            if incoming.source == SlotSource.USER:
                raise TaskIdentityOverrideError("model patch cannot claim user provenance")
            updates[name] = merge_slot(getattr(self, name), incoming)
        updates["updated_at"] = _utc_now()
        return self.model_copy(update=updates)

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"created_at", "updated_at"})
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def slot_fingerprint(self) -> str:
        payload = {name: slot.model_dump(mode="json") for name, slot in self.slot_states().items()}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def migrate_task_envelope(raw: dict[str, Any]) -> TaskEnvelope:
    """Upgrade legacy task dictionaries and preserve unknown 1.x fields."""
    version = raw.get("schema_version")
    if version is None or version in {"0", "0.1"}:
        migrated = dict(raw)
        migrated["schema_version"] = SCHEMA_VERSION
        for name in TaskEnvelope.SLOT_NAMES:
            if name not in migrated:
                continue
            value = migrated[name]
            if isinstance(value, dict) and "status" in value:
                continue
            migrated[name] = (
                SlotState.unknown().model_dump(mode="json")
                if value is None
                else SlotState.from_value(
                    value,
                    source=SlotSource.USER,
                    confirmed=True,
                ).model_dump(mode="json")
            )
        return TaskEnvelope.model_validate(migrated)
    return TaskEnvelope.model_validate(raw)
