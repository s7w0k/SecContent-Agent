"""ReviewerAgent 集成测试（计划 §67 / §36 / §37 / §38 / §39）。

覆盖：
  - Reviewer 不直接改稿（不写工具，改稿一律外派 revise_fn）
  - 证据不足 → REVISE；严重问题 → BLOCK
  - 超最大轮次 → BLOCK
  - 产品 Claim 必须有 Evidence Grounding

运行（仓库根目录）:
    python -m pytest tests/unit/specialists/test_reviewer_agent.py --basetemp ./.pytest-tmp-x -q --no-header
"""

from __future__ import annotations

from typing import Any

from agent.specialists.reviewer_agent import (
    BLOCK_MAX_GROUNDED_RATIO,
    MAX_REVIEW_ROUNDS,
    REVISE_MIN_GROUNDED_RATIO,
    ReviewDecision,
    ReviewerAgent,
)


class _FakeReview:
    """模拟 review_draft（DraftReviewer Service）的只读返回值。"""

    def __init__(self, *, passed: bool = True, issues: list[dict[str, Any]] | None = None):
        self.passed = passed
        self.issues = issues or []


def _review_service(result: _FakeReview):
    def _svc(text: str) -> _FakeReview:
        return result

    return _svc


def _claim_audit(grounded_ratio: float, unsupported: int = 0) -> Any:
    def _audit(text: str) -> dict:
        return {"grounded_ratio": grounded_ratio, "unsupported": unsupported}

    return _audit


# ═══════════════════════════════════════════════════════════════
# 1. Reviewer 不直接改稿
# ═══════════════════════════════════════════════════════════════


async def test_reviewer_cannot_mutate_draft():
    revoked = _FakeReview(passed=True, issues=[])
    agent = ReviewerAgent(
        review_service=_review_service(revoked),
        claim_audit=_claim_audit(1.0),
    )
    draft_text = "产品 PR 稿原文，不包含证据。"
    decision = await agent.review(draft_text=draft_text)

    # Reviewer 是只读的：不产生任何改稿请求，Draft 内容保持不变
    assert decision.status == "APPROVE"
    assert agent.revision_requests == []
    assert draft_text == "产品 PR 稿原文，不包含证据。"  # 未被修改


async def test_revision_only_via_external_fn():
    """改稿只能通过外部 revise_fn（DraftRevisionSkill）发生，而不是 Reviewer 自身。"""
    mutated = {"text": "v1"}
    revised_by: dict[str, int] = {"count": 0}
    agent = ReviewerAgent(
        review_service=_review_service(_FakeReview(passed=True, issues=[])),
        claim_audit=_claim_audit(0.6, unsupported=1),  # 一直 REVISE
        max_review_rounds=1,
    )

    def _get_text() -> str:
        return mutated["text"]

    async def _revise(decision: ReviewDecision, text: str) -> str:
        revised_by["count"] += 1
        mutated["text"] = f"v{revised_by['count'] + 1}"  # 改稿在外部发生
        return mutated["text"]

    final = await agent.review_loop(get_text=_get_text, revise=_revise)
    # Reviewer 确实发起了外派改稿，但最终仍不达标 -> 超限 BLOCK
    assert final.status == "BLOCK"
    assert revised_by["count"] == 1  # 仅一轮（max_review_rounds=1）
    assert len(agent.revision_requests) == 1  # 记录的是外派请求，非自身改稿


# ═══════════════════════════════════════════════════════════════
# 2. 证据不足 → REVISE
# ═══════════════════════════════════════════════════════════════


async def test_reviewer_requests_revision():
    agent = ReviewerAgent(
        review_service=_review_service(_FakeReview(passed=True, issues=[])),
        claim_audit=_claim_audit(0.7, unsupported=1),  # 0.7 < 0.8 → REVISE
    )
    decision = await agent.review(draft_text="含证据的部分")
    assert decision.status == "REVISE"
    assert decision.revision_instructions  # 给出修订指令


async def test_reviewer_approves_when_grounded():
    agent = ReviewerAgent(
        review_service=_review_service(_FakeReview(passed=True, issues=[])),
        claim_audit=_claim_audit(REVISE_MIN_GROUNDED_RATIO),
    )
    decision = await agent.review(draft_text="fully grounded")
    assert decision.status == "APPROVE"


# ═══════════════════════════════════════════════════════════════
# 3. 超最大轮次 → BLOCK
# ═══════════════════════════════════════════════════════════════


async def test_reviewer_blocks_after_max_rounds():
    agent = ReviewerAgent(
        review_service=_review_service(_FakeReview(passed=True, issues=[])),
        claim_audit=_claim_audit(0.6, unsupported=1),  # 始终 REVISE
        max_review_rounds=MAX_REVIEW_ROUNDS,  # =2
    )

    def _get_text() -> str:
        return "draft"

    async def _revise(decision: ReviewDecision, text: str) -> str:
        return text + "|revised"

    final = await agent.review_loop(get_text=_get_text, revise=_revise)
    assert final.status == "BLOCK"
    assert len(agent.revision_requests) == MAX_REVIEW_ROUNDS  # 只允许 2 轮
    assert "人工" in final.reason_summary


# ═══════════════════════════════════════════════════════════════
# 4. 产品 Claim 必须有 Evidence Grounding
# ═══════════════════════════════════════════════════════════════


async def test_product_claim_requires_evidence():
    agent = ReviewerAgent(
        review_service=_review_service(_FakeReview(passed=True, issues=[])),
        claim_audit=_claim_audit(0.0, unsupported=2),  # 含产品声明但无证据
    )
    decision = await agent.review(draft_text="本产品支持实时身份威胁检测…")
    assert decision.status == "BLOCK"  # 无证据支撑的产品声明不得放行
    assert decision.revision_instructions


async def test_critical_issue_blocks_even_if_grounded():
    agent = ReviewerAgent(
        review_service=_review_service(
            _FakeReview(
                passed=False,
                issues=[{"severity": "critical", "code": "compliance", "message": "涉红线"}],
            )
        ),
        claim_audit=_claim_audit(1.0),
    )
    decision = await agent.review(draft_text="x")
    assert decision.status == "BLOCK"
    assert decision.severity == "critical"
    assert "compliance" in decision.issue_refs


# ═══════════════════════════════════════════════════════════════
# 5. 阈值常量引用（防止被意外改写破坏判定闭环）
# ═══════════════════════════════════════════════════════════════


def test_decide_boundaries_are_sane():
    assert 0 < BLOCK_MAX_GROUNDED_RATIO < REVISE_MIN_GROUNDED_RATIO <= 1.0
    assert MAX_REVIEW_ROUNDS == 2
