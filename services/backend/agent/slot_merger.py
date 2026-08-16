"""Multi-turn slot merger and downstream invalidation graph."""

from __future__ import annotations

from typing import Any

from agent.contracts.task import SlotSource, SlotState, SlotStatus, TaskAssumption, TaskEnvelope, merge_slot
from agent.task_understanding import TaskEnvelopePatch
from pydantic import BaseModel, Field

INVALIDATION_GRAPH: dict[str, tuple[str, ...]] = {
    "intent": ("plan", "search_news", "classify_article", "match_products", "score_article", "generate_draft", "review_draft", "save_draft_version", "export_draft"),
    "news_query": ("search_news", "candidate_selection", "classify_article", "match_products", "score_article", "generate_draft", "review_draft"),
    "selected_article_ids": ("candidate_selection", "classify_article", "match_products", "score_article", "generate_draft", "review_draft", "save_draft_version", "export_draft"),
    "category": ("match_products", "score_article", "generate_draft", "review_draft"),
    "product_ids": ("score_article", "generate_draft", "review_draft", "save_draft_version", "export_draft"),
    "template_key": ("generate_draft", "review_draft", "save_draft_version", "export_draft"),
    "angle": ("generate_draft", "review_draft", "save_draft_version", "export_draft"),
    "tone": ("generate_draft", "review_draft", "save_draft_version", "export_draft"),
    "length": ("generate_draft", "review_draft", "save_draft_version", "export_draft"),
    "constraints": ("score_article", "generate_draft", "review_draft", "save_draft_version", "export_draft"),
    "save_policy": ("save_draft_version", "export_draft"),
    "requested_outputs": ("export_draft",),
    "draft_artifact": ("revise_draft", "review_draft", "save_draft_version", "export_draft"),
    "draft_version": ("revise_draft", "save_draft_version", "export_draft"),
    "revision_instruction": ("revise_draft", "review_draft"),
    "save_confirmed": ("save_draft_version",),
    "crawl_approved": ("crawl_news", "search_news", "candidate_selection"),
    "auto_select": ("candidate_selection",),
}


class SlotMergeResult(BaseModel):
    envelope: TaskEnvelope
    changed_slots: list[str] = Field(default_factory=list)
    invalidated_steps: list[str] = Field(default_factory=list)
    conflicted_slots: list[str] = Field(default_factory=list)


class SlotMerger:
    def merge(
        self,
        envelope: TaskEnvelope,
        patch: TaskEnvelopePatch | dict[str, Any],
        *,
        turn_id: str,
        source: SlotSource = SlotSource.USER,
        completed_steps: set[str] | None = None,
    ) -> SlotMergeResult:
        parsed = patch if isinstance(patch, TaskEnvelopePatch) else TaskEnvelopePatch.model_validate(patch)
        updates: dict[str, SlotState] = {}
        changed: list[str] = []
        conflicted: list[str] = []
        for name, value in parsed.slot_values().items():
            effective_source = source
            confirmed = source == SlotSource.USER and name in parsed.explicit_slots
            if source == SlotSource.USER and not confirmed:
                effective_source = SlotSource.MODEL
            incoming = SlotState.from_value(
                value,
                source=effective_source,
                confidence=1.0 if confirmed else parsed.confidence,
                turn_id=turn_id,
                confirmed=confirmed,
            )
            current = getattr(envelope, name)
            merged = merge_slot(current, incoming)
            updates[name] = merged
            if current.value != merged.value or current.status != merged.status:
                changed.append(name)
            if merged.status == SlotStatus.CONFLICTED:
                conflicted.append(name)
        if parsed.assumptions:
            merged = [*envelope.assumptions, *parsed.assumptions][-30:]
            seen: set[str] = set()
            unique: list[TaskAssumption] = []
            for item in merged:
                if item.text in seen:
                    continue
                seen.add(item.text)
                unique.append(item)
            updates["assumptions"] = unique
        candidate = envelope.model_copy(update=updates)
        invalidated: set[str] = set()
        for name in changed:
            invalidated.update(INVALIDATION_GRAPH.get(name, ()))
        if completed_steps is not None:
            invalidated.intersection_update(completed_steps)
        return SlotMergeResult(
            envelope=candidate,
            changed_slots=changed,
            invalidated_steps=sorted(invalidated),
            conflicted_slots=sorted(set(conflicted)),
        )


def merge_task_envelope(
    envelope: TaskEnvelope,
    patch: TaskEnvelopePatch | dict[str, Any],
    *,
    turn_id: str,
    source: SlotSource = SlotSource.USER,
    completed_steps: set[str] | None = None,
) -> SlotMergeResult:
    return SlotMerger().merge(
        envelope, patch, turn_id=turn_id, source=source, completed_steps=completed_steps
    )
