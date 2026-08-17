"""Deterministic business branches for discovery, ranking, low scores and review repair."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from agent.contracts.task import TaskIntent
from pydantic import BaseModel, Field


class JourneyAction(StrEnum):
    CONTINUE = "continue"
    STOP_EXPLAINED = "stop_explained"
    ASK_USER = "ask_user"
    WAIT_APPROVAL = "wait_approval"


class JourneyBranch(BaseModel):
    action: JourneyAction
    reason_code: str
    message: str


def decide_low_score(*, intent: str, worth_writing: bool, user_requested_draft: bool) -> JourneyBranch:
    if worth_writing:
        return JourneyBranch(action=JourneyAction.CONTINUE, reason_code="score_passed", message="评分达到写稿门槛。")
    explicit = user_requested_draft or intent in {
        TaskIntent.GENERATE_DRAFT.value,
        TaskIntent.SEARCH_AND_DRAFT.value,
    }
    if explicit:
        return JourneyBranch(
            action=JourneyAction.CONTINUE,
            reason_code="low_score_user_goal_overrides_recommendation",
            message="评分偏低，但用户明确要求写稿，将保留提示并继续。",
        )
    return JourneyBranch(
        action=JourneyAction.STOP_EXPLAINED,
        reason_code="low_score_not_worth_writing",
        message="评分未达到值得写作的门槛，已停止写稿并保留评分依据。",
    )


class RankedCandidate(BaseModel):
    article_id: str
    title: str = ""
    total_score: float
    product_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    rank: int = 0


class StableCandidateRanker:
    def rank(self, candidates: list[dict[str, Any]], *, top_n: int = 3, max_candidates: int = 20) -> list[RankedCandidate]:
        if top_n < 1 or top_n > 10:
            raise ValueError("top_n must be between 1 and 10")
        bounded = candidates[:max_candidates]
        ranked = sorted(
            bounded,
            key=lambda item: (
                -float(item.get("total_score", 0)),
                str(item.get("published_at", "")),
                str(item.get("article_id", "")),
            ),
        )[:top_n]
        return [
            RankedCandidate(
                article_id=str(item["article_id"]),
                title=str(item.get("title", "")),
                total_score=float(item.get("total_score", 0)),
                product_ids=list(item.get("product_ids") or []),
                evidence=list(item.get("evidence") or []),
                rank=index,
            )
            for index, item in enumerate(ranked, 1)
        ]


class ReviewRepairDecision(BaseModel):
    action: str
    status: str
    reason_code: str


class BoundedReviewRepairPolicy:
    def __init__(self, *, max_auto_repairs: int = 1):
        self.max_auto_repairs = max_auto_repairs

    def decide(self, issues: list[dict[str, Any]], *, repair_count: int) -> ReviewRepairDecision:
        severe = any(str(item.get("severity")) in {"error", "critical", "high"} for item in issues)
        if severe:
            return ReviewRepairDecision(action="ask_user", status="needs_user_review", reason_code="high_risk_review_issue")
        if not issues:
            return ReviewRepairDecision(action="complete", status="review_passed", reason_code="review_passed")
        if repair_count < self.max_auto_repairs:
            return ReviewRepairDecision(action="auto_repair", status="needs_user_review", reason_code="bounded_auto_repair")
        return ReviewRepairDecision(action="stop", status="review_failed", reason_code="auto_repair_budget_exhausted")
