"""Human annotation schema and deterministic inter-reviewer agreement metrics."""

from __future__ import annotations

import statistics
from collections import defaultdict

from pydantic import BaseModel, Field


class QualityScores(BaseModel):
    classification_correct: bool
    product_relevance: int = Field(..., ge=0, le=4)
    score_in_expert_range: bool
    factual_completeness: int = Field(..., ge=0, le=4)
    citation_quality: int = Field(..., ge=0, le=4)
    structure_quality: int = Field(..., ge=0, le=4)
    product_accuracy: int = Field(..., ge=0, le=4)
    style_quality: int = Field(..., ge=0, le=4)
    promotion_risk: int = Field(..., ge=0, le=4)
    revision_instruction_following: int = Field(..., ge=0, le=4)
    non_target_preservation: int = Field(..., ge=0, le=4)


class ReviewerAnnotation(BaseModel):
    sample_id: str = Field(..., min_length=1, max_length=100)
    reviewer_id: str = Field(..., min_length=1, max_length=100)
    scores: QualityScores
    failure_tags: list[str] = Field(default_factory=list, max_length=20)


def calculate_agreement(annotations: list[ReviewerAnnotation]) -> dict[str, float | int]:
    """Return exact categorical agreement and numeric mean absolute difference."""
    grouped: dict[str, list[ReviewerAnnotation]] = defaultdict(list)
    for item in annotations:
        grouped[item.sample_id].append(item)
    pairs = [items[:2] for items in grouped.values() if len(items) >= 2]
    if not pairs:
        return {"paired_samples": 0, "categorical_agreement": 0.0, "numeric_mae": 0.0}
    categorical: list[bool] = []
    differences: list[float] = []
    numeric_fields = [
        "product_relevance",
        "factual_completeness",
        "citation_quality",
        "structure_quality",
        "product_accuracy",
        "style_quality",
        "promotion_risk",
        "revision_instruction_following",
        "non_target_preservation",
    ]
    for first, second in pairs:
        categorical.extend(
            [
                first.scores.classification_correct == second.scores.classification_correct,
                first.scores.score_in_expert_range == second.scores.score_in_expert_range,
            ]
        )
        differences.extend(
            abs(getattr(first.scores, field) - getattr(second.scores, field))
            for field in numeric_fields
        )
    return {
        "paired_samples": len(pairs),
        "categorical_agreement": round(sum(categorical) / len(categorical), 4),
        "numeric_mae": round(statistics.mean(differences), 4),
    }
