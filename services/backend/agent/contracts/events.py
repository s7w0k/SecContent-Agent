"""Shared, redacted event envelope and replay validation helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"

CORE_EVENT_TYPES = frozenset(
    {
        "turn_received",
        "task_understood",
        "clarification_requested",
        "plan_created",
        "plan_revised",
        "tool_started",
        "tool_succeeded",
        "tool_failed",
        "observation_recorded",
        "validation_failed",
        "approval_requested",
        "checkpoint_saved",
        "artifact_created",
        "task_completed",
        "task_stopped",
    }
)

_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|authorization|password|secret|token|prompt|thinking|chain[_ -]?of[_ -]?thought)",
    re.IGNORECASE,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def sanitize_event_payload(value: Any, *, depth: int = 0) -> Any:
    """Keep audit summaries while removing credentials, prompts and private reasoning."""
    if depth > 5:
        return "[truncated]"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            name = str(key)
            if _SENSITIVE_KEY.search(name):
                clean[name] = "[redacted]"
            else:
                clean[name] = sanitize_event_payload(item, depth=depth + 1)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [sanitize_event_payload(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


class EventEnvelope(BaseModel):
    """Minimum event fields shared by every execution mode."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = SCHEMA_VERSION
    event_id: str = Field(..., min_length=1, max_length=100)
    run_id: str = Field(..., min_length=1, max_length=100)
    turn_id: str = Field(default="", max_length=100)
    trace_id: str = Field(default="", max_length=100)
    sequence: int = Field(..., ge=1)
    event_type: str = Field(..., min_length=1, max_length=64)
    status: str = Field(default="", max_length=32)
    timestamp: datetime = Field(default_factory=_utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class TimelineValidation(BaseModel):
    valid: bool
    duplicate_event_ids: list[str] = Field(default_factory=list)
    duplicate_sequences: list[int] = Field(default_factory=list)
    missing_sequences: list[int] = Field(default_factory=list)
    out_of_order_event_ids: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)


def validate_event_timeline(events: list[EventEnvelope]) -> TimelineValidation:
    """Detect duplicate/out-of-order events and sequence gaps without replaying effects."""
    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    duplicate_ids: list[str] = []
    duplicate_sequences: list[int] = []
    out_of_order: list[str] = []
    previous = 0
    for event in events:
        if event.event_id in seen_ids:
            duplicate_ids.append(event.event_id)
        seen_ids.add(event.event_id)
        if event.sequence in seen_sequences:
            duplicate_sequences.append(event.sequence)
        seen_sequences.add(event.sequence)
        if event.sequence < previous:
            out_of_order.append(event.event_id)
        previous = max(previous, event.sequence)
    missing: list[int] = []
    if seen_sequences:
        missing = sorted(set(range(min(seen_sequences), max(seen_sequences) + 1)) - seen_sequences)
    trace_ids = sorted({event.trace_id for event in events if event.trace_id})
    valid = (
        not duplicate_ids
        and not duplicate_sequences
        and not missing
        and not out_of_order
        and len(trace_ids) <= 1
    )
    return TimelineValidation(
        valid=valid,
        duplicate_event_ids=duplicate_ids,
        duplicate_sequences=duplicate_sequences,
        missing_sequences=missing,
        out_of_order_event_ids=out_of_order,
        trace_ids=trace_ids,
    )


def rebuild_user_timeline(events: list[EventEnvelope]) -> list[dict[str, Any]]:
    """Build a deduplicated user-visible timeline; payload is redacted again on read."""
    visible: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in sorted(events, key=lambda item: item.sequence):
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        if event.event_type not in CORE_EVENT_TYPES:
            continue
        visible.append(
            {
                "event_id": event.event_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "status": event.status,
                "timestamp": event.timestamp.isoformat(),
                "payload": sanitize_event_payload(event.payload),
            }
        )
    return visible
