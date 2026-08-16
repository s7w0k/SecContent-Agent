"""Deterministic slot merge and clarification rules."""

from __future__ import annotations

from dataclasses import dataclass

from agent.contracts.task import SlotState, SlotStatus, TaskEnvelope, TaskIntent, merge_slot


@dataclass(frozen=True)
class IntentSlotPolicy:
    required: tuple[str, ...] = ("goal",)
    required_any: tuple[tuple[str, ...], ...] = ()
    inferable: tuple[str, ...] = ()
    confirm_before_write: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlotDecision:
    missing_slots: tuple[str, ...]
    ambiguous_slots: tuple[str, ...]
    confirmation_slots: tuple[str, ...]
    questions: tuple[str, ...]
    can_start_write: bool


POLICIES: dict[TaskIntent, IntentSlotPolicy] = {
    TaskIntent.UNKNOWN: IntentSlotPolicy(required=("goal",)),
    TaskIntent.GENERATE_DRAFT: IntentSlotPolicy(
        required=("goal",),
        required_any=(("selected_article_ids", "news_query"),),
        inferable=("category", "product_ids", "template_key", "angle", "tone", "length"),
        confirm_before_write=("save_policy",),
    ),
    TaskIntent.SEARCH_AND_DRAFT: IntentSlotPolicy(
        required=("goal", "news_query"),
        inferable=("category", "product_ids", "template_key", "angle", "tone", "length"),
        confirm_before_write=("save_policy",),
    ),
    TaskIntent.CURATE_NEWS: IntentSlotPolicy(
        required=("goal", "news_query"),
        inferable=("category", "product_ids"),
    ),
    TaskIntent.REVISE_DRAFT: IntentSlotPolicy(
        required=("goal", "selected_article_ids", "constraints"),
        inferable=("tone", "length"),
        confirm_before_write=("save_policy",),
    ),
    TaskIntent.REVIEW_DRAFT: IntentSlotPolicy(required=("goal", "selected_article_ids")),
    TaskIntent.SAVE_DRAFT: IntentSlotPolicy(
        required=("goal", "selected_article_ids", "save_policy"),
        confirm_before_write=("save_policy",),
    ),
    TaskIntent.EXPORT_DRAFT: IntentSlotPolicy(
        required=("goal", "selected_article_ids", "requested_outputs")
    ),
    TaskIntent.SEARCH_AND_RANK: IntentSlotPolicy(
        required=("goal", "news_query"), inferable=("category", "product_ids")
    ),
    TaskIntent.REVISE: IntentSlotPolicy(
        required=("goal", "selected_article_ids", "constraints"),
        inferable=("tone", "length"),
        confirm_before_write=("save_policy",),
    ),
    TaskIntent.SAVE: IntentSlotPolicy(
        required=("goal", "selected_article_ids", "save_policy"),
        confirm_before_write=("save_policy",),
    ),
    TaskIntent.ASK_STATUS: IntentSlotPolicy(required=("goal",)),
    TaskIntent.CANCEL: IntentSlotPolicy(required=("goal",)),
}

QUESTION_TEXT = {
    "goal": "你希望这次任务最终产出什么？",
    "news_query": "请提供要检索的新闻主题或关键词。",
    "selected_article_ids": "请指定要处理的文章，或提供可用于检索的新闻描述。",
    "constraints": "请说明需要修改的内容或必须保留的要求。",
    "save_policy": "是否确认把结果保存为新版本？",
}


def _is_available(slot: SlotState) -> bool:
    return slot.status in {SlotStatus.INFERRED, SlotStatus.CONFIRMED} and slot.value not in (
        None,
        "",
        [],
    )


def merge_envelope_slot(envelope: TaskEnvelope, name: str, incoming: SlotState) -> TaskEnvelope:
    if name not in TaskEnvelope.SLOT_NAMES:
        return envelope
    return envelope.model_copy(update={name: merge_slot(getattr(envelope, name), incoming)})


def decide_slots(envelope: TaskEnvelope, *, max_questions: int = 2) -> SlotDecision:
    """Apply intent rules without asking for fields that tools can infer."""
    try:
        intent = TaskIntent(envelope.intent.value or TaskIntent.UNKNOWN)
    except ValueError:
        intent = TaskIntent.UNKNOWN
    policy = POLICIES[intent]
    missing: list[str] = []
    ambiguous = [
        name
        for name, slot in envelope.slot_states().items()
        if slot.status == SlotStatus.CONFLICTED
    ]
    for name in policy.required:
        if not _is_available(getattr(envelope, name)):
            missing.append(name)
    for group in policy.required_any:
        if not any(_is_available(getattr(envelope, name)) for name in group):
            missing.append(group[0])
    confirmation: list[str] = []
    for name in policy.confirm_before_write:
        slot = getattr(envelope, name)
        if _is_available(slot) and slot.status != SlotStatus.CONFIRMED:
            confirmation.append(name)
    ordered = [*ambiguous, *missing, *confirmation]
    questions = tuple(
        QUESTION_TEXT.get(name, f"请确认 {name}。") for name in ordered[: max(0, max_questions)]
    )
    blocking = set(missing) | set(ambiguous) | set(confirmation)
    return SlotDecision(
        missing_slots=tuple(missing),
        ambiguous_slots=tuple(ambiguous),
        confirmation_slots=tuple(confirmation),
        questions=questions,
        can_start_write=not blocking,
    )
