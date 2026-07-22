"""任务 11.1：简单审核模型、规则常量和正文哈希测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agent.draft_reviewer import (
    ABSOLUTE_WORDS,
    COMPARISON_WORDS,
    GUARANTEE_WORDS,
    compute_content_hash,
)
from models.draft_review import ISSUE_CATEGORIES, ISSUE_SEVERITIES, DraftReview
from pydantic import ValidationError

CONTENT_HASH = "a" * 64
REVIEWED_AT = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)


def _issue(**overrides):
    issue = {
        "issue_id": "issue-001",
        "category": "guarantee_claim",
        "severity": "high",
        "quote": "实现100%安全防护",
        "reason": "对安全效果作出绝对保证",
        "suggestion": "描述风险降低能力和适用范围",
        "suggested_rewrite": "帮助降低相关攻击风险",
    }
    issue.update(overrides)
    return issue


def test_complete_review_validates_and_serializes():
    review = DraftReview(
        status="completed",
        content_hash=CONTENT_HASH,
        summary="发现 1 个必须修改问题",
        issues=[_issue()],
        counts={"high": 1, "medium": 0, "low": 0},
        fact_check_available=True,
        reviewed_at=REVIEWED_AT,
    )

    assert review.issues[0].category == "guarantee_claim"
    assert review.counts == {"high": 1, "medium": 0, "low": 0}
    assert review.model_dump(mode="json")["reviewed_at"] == "2026-07-22T08:00:00Z"


def test_empty_completed_review_is_valid():
    review = DraftReview(
        status="completed",
        content_hash=CONTENT_HASH,
        summary="未发现需要修改的问题",
        issues=[],
        counts={"high": 0, "medium": 0, "low": 0},
        fact_check_available=True,
        reviewed_at=REVIEWED_AT,
    )

    assert review.issues == []
    assert sum(review.counts.values()) == 0


def test_failed_review_is_valid_without_issues():
    review = DraftReview(
        status="failed",
        content_hash=CONTENT_HASH,
        summary="检查失败",
        issues=[],
        counts={"high": 0, "medium": 0, "low": 0},
        fact_check_available=False,
        error="模型响应超时",
        reviewed_at=REVIEWED_AT,
    )

    assert review.status == "failed"
    assert review.error == "模型响应超时"


def test_model_rejects_unknown_category_and_inconsistent_counts():
    with pytest.raises(ValidationError):
        DraftReview(
            status="completed",
            content_hash=CONTENT_HASH,
            summary="结果",
            issues=[_issue(category="legal_approval")],
            counts={"high": 1, "medium": 0, "low": 0},
            fact_check_available=True,
        )

    with pytest.raises(ValidationError, match="counts must match issues"):
        DraftReview(
            status="completed",
            content_hash=CONTENT_HASH,
            summary="结果",
            issues=[_issue()],
            counts={"high": 0, "medium": 1, "low": 0},
            fact_check_available=True,
        )


def test_three_severities_and_ten_issue_categories_are_fixed():
    assert ISSUE_SEVERITIES == ("high", "medium", "low")
    assert len(ISSUE_CATEGORIES) == 10
    assert "absolute_claim" in ISSUE_CATEGORIES
    assert "competitor_comparison" in ISSUE_CATEGORIES
    assert "guarantee_claim" in ISSUE_CATEGORIES


def test_keyword_groups_cover_required_wording_risks():
    assert {"业内第一", "唯一", "遥遥领先"}.issubset(ABSOLUTE_WORDS)
    assert {"比", "碾压", "超越"}.issubset(COMPARISON_WORDS)
    assert {"100%", "零风险", "彻底杜绝"}.issubset(GUARANTEE_WORDS)


def test_content_hash_normalizes_line_endings_and_outer_whitespace():
    windows_text = "  第一行\r\n第二行\r\n"
    unix_text = "第一行\n第二行"

    assert compute_content_hash(windows_text) == compute_content_hash(unix_text)
    assert len(compute_content_hash(unix_text)) == 64
    assert compute_content_hash("第一行 第二行") != compute_content_hash(unix_text)
