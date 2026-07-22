"""稿件内容与宣传话术检查结果模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

type IssueSeverity = Literal["high", "medium", "low"]
type IssueCategory = Literal[
    "fact_mismatch",
    "unsupported_claim",
    "internal_conflict",
    "absolute_claim",
    "competitor_comparison",
    "competitor_disparagement",
    "guarantee_claim",
    "unsupported_data",
    "exaggerated_claim",
    "ambiguous_expression",
]
type DraftReviewStatus = Literal["completed", "failed", "partial"]

ISSUE_SEVERITIES: tuple[IssueSeverity, ...] = ("high", "medium", "low")
ISSUE_CATEGORIES: tuple[IssueCategory, ...] = (
    "fact_mismatch",
    "unsupported_claim",
    "internal_conflict",
    "absolute_claim",
    "competitor_comparison",
    "competitor_disparagement",
    "guarantee_claim",
    "unsupported_data",
    "exaggerated_claim",
    "ambiguous_expression",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DraftReviewIssue(BaseModel):
    """稿件中一处可定位、可解释、可修改的问题。"""

    issue_id: str = Field(min_length=1, max_length=100)
    category: IssueCategory
    severity: IssueSeverity
    quote: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)
    suggested_rewrite: str | None = None


class DraftReview(BaseModel):
    """单篇稿件的内容与宣传话术检查结果。"""

    status: DraftReviewStatus
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary: str = Field(min_length=1)
    issues: list[DraftReviewIssue] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=lambda: dict.fromkeys(ISSUE_SEVERITIES, 0))
    fact_check_available: bool
    error: str | None = None
    reviewed_at: datetime = Field(default_factory=_utc_now)

    @field_validator("counts")
    @classmethod
    def validate_counts(cls, value: dict[str, int]) -> dict[str, int]:
        """计数只允许 high/medium/low，且不能为负数。"""

        expected_keys = set(ISSUE_SEVERITIES)
        if set(value) != expected_keys:
            raise ValueError("counts must contain exactly high, medium and low")
        if any(count < 0 for count in value.values()):
            raise ValueError("counts cannot contain negative values")
        return value

    @model_validator(mode="after")
    def validate_result_consistency(self) -> DraftReview:
        """避免保存问题列表、计数或失败状态互相矛盾的结果。"""

        actual_counts = {
            severity: sum(issue.severity == severity for issue in self.issues)
            for severity in ISSUE_SEVERITIES
        }
        if self.counts != actual_counts:
            raise ValueError("counts must match issues")
        if self.status == "failed" and not self.error:
            raise ValueError("failed review must include error")
        if self.status == "completed" and self.error:
            raise ValueError("completed review cannot include error")
        return self
