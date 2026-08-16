"""Prioritized, non-repeating clarification policy with visible assumptions."""

from __future__ import annotations

from agent.contracts.task import SlotStatus, TaskAssumption, TaskEnvelope, TaskIntent
from pydantic import BaseModel, Field


class ClarificationQuestion(BaseModel):
    slot: str
    question: str
    reason: str
    priority: int = Field(ge=1, le=100)


class ClarificationDecision(BaseModel):
    should_ask: bool
    questions: list[ClarificationQuestion] = Field(default_factory=list, max_length=3)
    assumptions: list[TaskAssumption] = Field(default_factory=list)
    skipped_previously_asked: list[str] = Field(default_factory=list)
    blocked_slots: list[str] = Field(default_factory=list)

    @property
    def visible_assumptions(self) -> list[str]:
        return [item.text for item in self.assumptions]

    @property
    def can_proceed(self) -> bool:
        return not self.blocked_slots


_QUESTION_TEXT = {
    "goal": "你希望这次任务最终产出什么？",
    "news_query": "要检索哪条新闻或什么主题？",
    "selected_article_ids": "候选新闻不唯一，请选择要处理的新闻。",
    "product_ids": "这篇内容要关联哪个产品？",
    "constraints": "请说明要修改的内容，以及必须保留的部分。",
    "save_policy": "是否确认保存为新的业务版本？",
    "requested_outputs": "需要导出为 Markdown、Word 还是 PDF？",
}

_PRIORITY = {
    "selected_article_ids": 100,
    "product_ids": 90,
    "constraints": 85,
    "save_policy": 80,
    "news_query": 75,
    "requested_outputs": 70,
    "goal": 60,
}


class ClarificationPolicy:
    """Asks only blocking questions; read-only inferable slots are deliberately omitted."""

    def decide(
        self,
        envelope: TaskEnvelope,
        *,
        asked_slots: set[str] | None = None,
        answered_slots: set[str] | None = None,
        max_questions: int = 3,
        candidate_count: int | None = None,
        allow_defaults: bool = False,
    ) -> ClarificationDecision:
        asked = set(asked_slots or ())
        answered = set(answered_slots or ())
        intent = TaskIntent(envelope.intent.value or TaskIntent.UNKNOWN)
        needed: list[tuple[str, str]] = []

        if not self._available(envelope.goal):
            needed.append(("goal", "task objective is missing"))
        if intent in {TaskIntent.SEARCH_AND_RANK, TaskIntent.SEARCH_AND_DRAFT, TaskIntent.CURATE_NEWS} and not self._available(envelope.news_query):
            needed.append(("news_query", "a search query is required"))
        if intent in {TaskIntent.GENERATE_DRAFT, TaskIntent.REVISE, TaskIntent.REVISE_DRAFT, TaskIntent.SAVE, TaskIntent.SAVE_DRAFT, TaskIntent.EXPORT_DRAFT} and not self._available(envelope.selected_article_ids):
            needed.append(("selected_article_ids", "the target article is ambiguous"))
        if candidate_count is not None and candidate_count > 1 and not self._available(envelope.selected_article_ids):
            needed.append(("selected_article_ids", "multiple similar candidates remain"))
        if intent in {TaskIntent.REVISE, TaskIntent.REVISE_DRAFT} and not self._available(envelope.constraints):
            needed.append(("constraints", "revision instructions are required"))
        if (
            intent in {TaskIntent.SAVE, TaskIntent.SAVE_DRAFT}
            and envelope.save_policy.status != SlotStatus.CONFIRMED
        ):
            needed.append(("save_policy", "saving a business version requires confirmation"))
        if intent == TaskIntent.EXPORT_DRAFT and not self._available(envelope.requested_outputs):
            needed.append(("requested_outputs", "the export format is missing"))

        # Conflicts are more important than ordinary missing values.
        for name, slot in envelope.slot_states().items():
            if slot.status == SlotStatus.CONFLICTED:
                needed.append((name, "new evidence conflicts with a confirmed value"))

        if allow_defaults:
            defaultable = {"product_ids", "requested_outputs"}
            needed = [(slot, reason) for slot, reason in needed if slot not in defaultable]

        unique: dict[str, str] = {}
        for slot, reason in needed:
            if slot not in answered:
                unique.setdefault(slot, reason)
        skipped = sorted(slot for slot in unique if slot in asked)
        candidates = [
            ClarificationQuestion(
                slot=slot,
                question=_QUESTION_TEXT.get(slot, f"请确认 {slot}。"),
                reason=reason,
                priority=_PRIORITY.get(slot, 50) + (20 if "conflicts" in reason else 0),
            )
            for slot, reason in unique.items()
            if slot not in asked
        ]
        candidates.sort(key=lambda item: (-item.priority, item.slot))
        questions = candidates[: max(1, min(max_questions, 3))]
        return ClarificationDecision(
            should_ask=bool(questions),
            questions=questions,
            assumptions=list(envelope.assumptions),
            skipped_previously_asked=skipped,
            blocked_slots=sorted(unique),
        )

    @staticmethod
    def _available(slot) -> bool:
        return slot.status in {SlotStatus.INFERRED, SlotStatus.CONFIRMED} and slot.value not in (
            None,
            "",
            [],
        )
