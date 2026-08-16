from __future__ import annotations

import json

import pytest
from agent.contracts.task import (
    RiskLevel,
    SlotSource,
    SlotState,
    SlotStatus,
    TaskEnvelope,
    TaskIdentityOverrideError,
    TaskIntent,
    merge_slot,
    migrate_task_envelope,
)
from agent.slot_policy import decide_slots
from pydantic import ValidationError


def _envelope(**updates) -> TaskEnvelope:
    base = TaskEnvelope.from_user_input(
        task_id="task-1",
        thread_id="thread-1",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="生成一篇稿件",
        intent=TaskIntent.GENERATE_DRAFT,
        acceptance_criteria=["引用文章证据"],
        turn_id="turn-1",
    )
    return base.model_copy(update=updates)


@pytest.mark.parametrize("index", range(50))
def test_task_envelope_round_trip_matrix(index: int):
    envelope = _envelope(
        selected_article_ids=SlotState.from_value(
            [f"article-{index}"], source=SlotSource.USER, turn_id=f"turn-{index}"
        ),
        angle=SlotState.from_value(
            f"angle-{index}", source=SlotSource.MODEL, confidence=(index + 1) / 51
        ),
    )
    restored = migrate_task_envelope(json.loads(envelope.model_dump_json()))
    assert restored == envelope
    assert restored.fingerprint() == envelope.fingerprint()
    assert restored.slot_fingerprint() == envelope.slot_fingerprint()


def test_forward_compatible_unknown_field_is_preserved():
    raw = _envelope().model_dump(mode="json")
    raw["future_capability"] = {"version": 2}
    restored = migrate_task_envelope(raw)
    assert restored.model_extra == {"future_capability": {"version": 2}}


@pytest.mark.parametrize(
    ("field", "value"),
    [("intent", "delete_database"), ("risk_level", "extreme"), ("save_policy", "overwrite")],
)
def test_illegal_slot_enum_is_rejected(field: str, value: str):
    raw = _envelope().model_dump(mode="json")
    raw[field] = SlotState.from_value(value, source=SlotSource.MODEL).model_dump(mode="json")
    with pytest.raises(ValidationError):
        TaskEnvelope.model_validate(raw)


def test_oversized_slot_text_is_rejected():
    with pytest.raises(ValidationError):
        SlotState.from_value("x" * 4001, source=SlotSource.USER)


@pytest.mark.parametrize("protected", ["task_id", "thread_id", "user_id", "tenant_id"])
def test_model_cannot_override_server_identity(protected: str):
    with pytest.raises(TaskIdentityOverrideError):
        _envelope().apply_model_patch({protected: "attacker"})


def test_model_cannot_claim_user_provenance():
    with pytest.raises(TaskIdentityOverrideError):
        _envelope().apply_model_patch(
            {
                "tone": {
                    "value": "urgent",
                    "status": "confirmed",
                    "source": "user",
                    "confidence": 1.0,
                }
            }
        )


def test_confirmed_user_slot_survives_lower_priority_conflict():
    current = SlotState.from_value("ai-bom", source=SlotSource.USER, turn_id="t1")
    incoming = SlotState.from_value(
        "gateway", source=SlotSource.MODEL, confidence=0.9, turn_id="t2"
    )
    merged = merge_slot(current, incoming)
    assert merged.value == "ai-bom"
    assert merged.status == SlotStatus.CONFLICTED
    assert merged.conflicts[0].value == "gateway"


def test_latest_explicit_user_correction_wins():
    current = SlotState.from_value("long", source=SlotSource.USER, turn_id="t1")
    incoming = SlotState.from_value("short", source=SlotSource.USER, turn_id="t2")
    merged = merge_slot(current, incoming)
    assert merged.value == "short"
    assert merged.status == SlotStatus.CONFIRMED
    assert merged.conflicts == []


def test_missing_article_or_query_blocks_write_and_asks_at_most_two_questions():
    decision = decide_slots(_envelope(), max_questions=2)
    assert "selected_article_ids" in decision.missing_slots
    assert decision.can_start_write is False
    assert len(decision.questions) <= 2


def test_inferable_product_is_not_mechanically_asked():
    envelope = _envelope(
        selected_article_ids=SlotState.from_value(["art-1"], source=SlotSource.USER),
        risk_level=SlotState.from_value(RiskLevel.LOW.value, source=SlotSource.MODEL),
    )
    decision = decide_slots(envelope)
    assert "product_ids" not in decision.missing_slots
    assert all("product" not in question.lower() for question in decision.questions)
