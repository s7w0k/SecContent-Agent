from __future__ import annotations

from agent.contracts.events import (
    EventEnvelope,
    rebuild_user_timeline,
    sanitize_event_payload,
    validate_event_timeline,
)
from agent.runtime_events import RuntimeEventStore


def _event(event_id: str, sequence: int, event_type: str = "tool_started") -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        run_id="run-1",
        turn_id="turn-1",
        trace_id="trace-1",
        sequence=sequence,
        event_type=event_type,
        payload={"summary": "ok"},
    )


def test_event_payload_redacts_secret_prompt_and_private_reasoning():
    clean = sanitize_event_payload(
        {
            "api_key": "credential-value",
            "system_prompt": "internal",
            "chain_of_thought": "private",
            "summary": "x" * 800,
        }
    )
    assert clean["api_key"] == "[redacted]"
    assert clean["system_prompt"] == "[redacted]"
    assert clean["chain_of_thought"] == "[redacted]"
    assert len(clean["summary"]) == 500


def test_timeline_validation_detects_duplicate_gap_and_out_of_order():
    result = validate_event_timeline([_event("e1", 1), _event("e3", 3), _event("e1", 2)])
    assert result.valid is False
    assert result.duplicate_event_ids == ["e1"]
    assert result.out_of_order_event_ids == ["e1"]


def test_user_timeline_deduplicates_and_filters_internal_events():
    events = [
        _event("e1", 1, "turn_received"),
        _event("e1", 1, "turn_received"),
        _event("e2", 2, "internal_debug"),
        _event("e3", 3, "task_completed"),
    ]
    timeline = rebuild_user_timeline(events)
    assert [item["event_type"] for item in timeline] == ["turn_received", "task_completed"]


def test_timeline_rejects_trace_fork_within_one_run():
    first = _event("e1", 1)
    second = _event("e2", 2).model_copy(update={"trace_id": "trace-2"})
    result = validate_event_timeline([first, second])
    assert result.valid is False
    assert result.trace_ids == ["trace-1", "trace-2"]


class _Collection:
    def __init__(self):
        self.docs = []

    async def find_one(self, query, sort=None):
        matched = [doc for doc in self.docs if all(doc.get(k) == v for k, v in query.items())]
        if not matched:
            return None
        if sort:
            key, direction = sort[0]
            return sorted(matched, key=lambda item: item.get(key, 0), reverse=direction < 0)[0]
        return matched[0]

    async def insert_one(self, doc):
        self.docs.append(doc)


class _DB(dict):
    def __getitem__(self, key):
        if key not in self:
            self[key] = _Collection()
        return super().__getitem__(key)


async def test_runtime_event_deduplication_key_returns_existing_event():
    store = RuntimeEventStore(_DB())
    first = await store.append(
        run_id="run-1",
        turn_id="turn-1",
        trace_id="trace-1",
        event_type="tool_started",
        payload={"prompt": "private", "tool": "get_article"},
        deduplication_key="step-1:start",
    )
    second = await store.append(
        run_id="run-1",
        event_type="tool_started",
        deduplication_key="step-1:start",
    )
    assert second.event_id == first.event_id
    assert len(store.col.docs) == 1
    assert first.payload["prompt"] == "[redacted]"
