"""Governed memory, feedback and candidate promotion contracts.

This module keeps learning signals outside the executable production registry.
Automatic behavior may create a draft candidate, but only an evaluated,
approved release can be handed to the existing evolution Publisher.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryLayer(StrEnum):
    SESSION = "session"
    USER = "user"
    ORGANIZATION = "organization"


_MEMORY_PRIORITY = {
    MemoryLayer.USER: 100,
    MemoryLayer.SESSION: 200,
    MemoryLayer.ORGANIZATION: 300,
}


class GovernedMemory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_id: str = Field(default_factory=lambda: f"mem-{uuid4().hex[:12]}")
    tenant_id: str
    user_id: str = ""
    layer: MemoryLayer
    key: str
    value: Any
    provenance: str = Field(..., min_length=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_owner(self) -> GovernedMemory:
        if self.layer != MemoryLayer.ORGANIZATION and not self.user_id:
            raise ValueError("session and user memory require user_id")
        return self


class GovernedMemoryStore:
    """Tenant-isolated memory with explicit provenance and deterministic conflict rules."""

    def __init__(self) -> None:
        self._items: dict[str, GovernedMemory] = {}
        self._deleted: set[str] = set()

    def put(self, item: GovernedMemory) -> GovernedMemory:
        self._items[item.memory_id] = item
        return item

    def delete(self, *, memory_id: str, tenant_id: str, user_id: str) -> bool:
        item = self._items.get(memory_id)
        if item is None or item.tenant_id != tenant_id:
            return False
        if item.layer == MemoryLayer.ORGANIZATION or item.user_id != user_id:
            return False
        self._deleted.add(memory_id)
        return True

    def resolve(self, *, tenant_id: str, user_id: str, key: str) -> GovernedMemory | None:
        now = datetime.now(UTC)
        candidates = [
            item
            for item in self._items.values()
            if item.memory_id not in self._deleted
            and item.tenant_id == tenant_id
            and item.key == key
            and (item.layer == MemoryLayer.ORGANIZATION or item.user_id == user_id)
            and (item.expires_at is None or item.expires_at > now)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (_MEMORY_PRIORITY[item.layer], item.confidence, item.created_at),
        )


class FeedbackAction(StrEnum):
    ACCEPT_CANDIDATE = "accept_candidate"
    REJECT_CANDIDATE = "reject_candidate"
    MANUAL_EDIT = "manual_edit"
    SELECT_VERSION = "select_version"
    CORRECT_SCORE = "correct_score"
    SAVE = "save"
    ABANDON = "abandon"


_SENSITIVE_PAYLOAD_KEYS = {"content", "body", "raw_text", "article_content", "draft_content"}


class LearningFeedbackEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: f"feedback-{uuid4().hex[:12]}")
    idempotency_key: str
    tenant_id: str
    user_id: str
    run_id: str
    action: FeedbackAction
    target_ref: str
    context: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0, le=1)
    authorized_sensitive_capture: bool = False
    undone_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FeedbackLedger:
    """Append-only, idempotent learning events with reversible interpretation."""

    def __init__(self) -> None:
        self._events: dict[str, LearningFeedbackEvent] = {}
        self._keys: dict[tuple[str, str], str] = {}

    @staticmethod
    def _minimize(context: dict[str, Any], *, authorized: bool) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in context.items():
            if key.lower() in _SENSITIVE_PAYLOAD_KEYS and not authorized:
                digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
                result[f"{key}_hash"] = f"sha256:{digest}"
            else:
                result[key] = value
        return result

    def append(self, event: LearningFeedbackEvent) -> LearningFeedbackEvent:
        unique = (event.tenant_id, event.idempotency_key)
        if unique in self._keys:
            return self._events[self._keys[unique]]
        clean = event.model_copy(
            update={
                "context": self._minimize(
                    event.context, authorized=event.authorized_sensitive_capture
                )
            }
        )
        self._events[clean.event_id] = clean
        self._keys[unique] = clean.event_id
        return clean

    def undo(self, *, event_id: str, tenant_id: str, user_id: str) -> LearningFeedbackEvent:
        event = self._events[event_id]
        if event.tenant_id != tenant_id or event.user_id != user_id:
            raise PermissionError("feedback event is outside the caller boundary")
        updated = event.model_copy(update={"undone_at": datetime.now(UTC)})
        self._events[event_id] = updated
        return updated

    def active(self, *, tenant_id: str, user_id: str) -> list[LearningFeedbackEvent]:
        return [
            event
            for event in self._events.values()
            if event.tenant_id == tenant_id
            and event.user_id == user_id
            and event.undone_at is None
        ]


class SkillChangeCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(default_factory=lambda: f"candidate-{uuid4().hex[:12]}")
    target_type: str
    base_version: str
    source_dataset_id: str
    baseline_snapshot_id: str
    hypothesis: str = Field(..., min_length=12)
    target_failures: tuple[str, ...] = Field(..., min_length=1)
    expected_metrics: dict[str, float] = Field(..., min_length=1)
    content: dict[str, Any]
    status: str = "draft"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def enforce_draft(self) -> SkillChangeCandidate:
        if self.status != "draft":
            raise ValueError("automatic candidates must remain draft")
        return self


class DraftCandidateRegistry:
    def __init__(self) -> None:
        self._candidates: dict[str, SkillChangeCandidate] = {}

    def create(self, candidate: SkillChangeCandidate) -> SkillChangeCandidate:
        self._candidates[candidate.candidate_id] = candidate
        return candidate

    def executable(self, candidate_id: str) -> bool:
        return False


class PairedEvalGateReport(BaseModel):
    passed: bool
    sample_count: int
    mean_delta: float
    confidence_interval_95: tuple[float, float]
    gates: dict[str, bool]
    reason_codes: list[str] = Field(default_factory=list)


class PairedEvalGate:
    """Paired, repeatable gate: quality must improve without any hard regression."""

    @staticmethod
    def evaluate(
        *,
        baseline_quality: list[float],
        candidate_quality: list[float],
        baseline_security_failures: int,
        candidate_security_failures: int,
        baseline_factual_failures: int,
        candidate_factual_failures: int,
        format_success_delta: float = 0.0,
        token_cost_delta_ratio: float = 0.0,
        max_token_cost_delta_ratio: float = 0.10,
        leaked_case_ids: set[str] | None = None,
    ) -> PairedEvalGateReport:
        if not baseline_quality or len(baseline_quality) != len(candidate_quality):
            raise ValueError("paired eval requires equal non-empty samples")
        deltas = [
            candidate - baseline
            for baseline, candidate in zip(
                baseline_quality, candidate_quality, strict=True
            )
        ]
        mean = statistics.fmean(deltas)
        margin = 0.0
        if len(deltas) > 1:
            margin = 1.96 * statistics.stdev(deltas) / math.sqrt(len(deltas))
        interval = (mean - margin, mean + margin)
        gates = {
            "quality_improvement": interval[0] > 0,
            "security_non_regression": candidate_security_failures <= baseline_security_failures,
            "factual_non_regression": candidate_factual_failures <= baseline_factual_failures,
            "format_non_regression": format_success_delta >= 0,
            "cost_budget": token_cost_delta_ratio <= max_token_cost_delta_ratio,
            "no_data_leakage": not leaked_case_ids,
        }
        return PairedEvalGateReport(
            passed=all(gates.values()),
            sample_count=len(deltas),
            mean_delta=round(mean, 6),
            confidence_interval_95=(round(interval[0], 6), round(interval[1], 6)),
            gates=gates,
            reason_codes=[name for name, passed in gates.items() if not passed],
        )
