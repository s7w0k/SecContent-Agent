"""ReviewerAgent - 生成与验证分离的独立 Specialist Agent（计划 §35 / §36 / §37 / §38）。

核心不变量：
  - Reviewer 不直接改稿：只产出 ReviewDecision；改稿必须经外部 revise_fn（DraftRevisionSkill）。
  - 产物 Claim 必须有产品证据 Grounding（draft_claim_audit），否则 REVISE/BLOCK。
  - 轮次受限（MAX_REVIEW_ROUNDS=2），超限 BLOCK / 需人工。

Reviewer Agent
  ↓ read-only
review_draft Tool
  ↓
DraftReviewer Service
+ wiki/draft_claim_audit（产品 Claim → Evidence ID → Source Grounding）
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_REVIEW_ROUNDS = 2

# 章节 §39：Product Evidence Grounding 阈值
REVISE_MIN_GROUNDED_RATIO = 0.8  # grounded_ratio 低于此 → 至少 REVISE
BLOCK_MAX_GROUNDED_RATIO = 0.5  # grounded_ratio <= 此 → BLOCK
CRITICAL_SEVERITIES = {"critical", "error"}

ReviewStatus = Literal["APPROVE", "REVISE", "BLOCK"]


class ReviewDecision(BaseModel):
    """计划 §36：一次审核的结论。"""

    status: ReviewStatus
    severity: str = Field(default="info")
    issue_refs: list[str] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)
    reason_summary: str = Field(default="")


ReviewService = Callable[[str], Any]  # 输入草稿正文，返回带 .passed / .issues 的对象
ClaimAuditor = Callable[[str], dict]  # 输入草稿正文，返回 dict（grounded_ratio / unsupported）
ReviseFn = Callable[[ReviewDecision, str], str]  # 外部改稿（DraftRevisionSkill）


class ReviewerAgent:
    def __init__(
        self,
        *,
        review_service: ReviewService,
        claim_audit: ClaimAuditor,
        max_review_rounds: int = MAX_REVIEW_ROUNDS,
    ) -> None:
        self.review_service = review_service
        self.claim_audit = claim_audit
        self.max_review_rounds = max(1, int(max_review_rounds))
        # 审计轨迹：Reviewer 只记录"请求外部改稿"，自身永不写稿。
        self.revision_requests: list[dict[str, Any]] = []

    # ── 单轮审核 ──────────────────────────────────────────

    async def review(self, *, draft_text: str) -> ReviewDecision:
        passed, issues = self._collect_review(draft_text)
        audit = self._collect_grounding(draft_text)
        grounded_ratio = float(audit.get("grounded_ratio", 0.0))
        unsupported = int(audit.get("unsupported", 0))
        return self._assess(
            passed=passed, issues=issues, grounded_ratio=grounded_ratio, unsupported=unsupported
        )

    # ── 多轮审核（改稿外派）──────────────────────────────

    async def review_loop(
        self,
        *,
        get_text: Callable[[], str],
        revise: ReviseFn,
    ) -> ReviewDecision:
        """Draft v1 → Review → (Revise v2) → Review → … 直到 APPROVE 或超限 BLOCK。

        改稿一律交给外部 `revise`（即 DraftRevisionSkill），Reviewer 本身不改稿。
        """
        text = get_text()
        decision = await self.review(draft_text=text)
        rounds = 0
        while decision.status == "REVISE" and rounds < self.max_review_rounds:
            rounds += 1
            self.revision_requests.append({"round": rounds, "decision": decision.model_dump()})
            text = await self._external_revise(revise, decision, text)
            decision = await self.review(draft_text=text)

        if decision.status == "REVISE" and rounds >= self.max_review_rounds:
            return ReviewDecision(
                status="BLOCK",
                severity="error",
                issue_refs=decision.issue_refs,
                revision_instructions=decision.revision_instructions,
                reason_summary="超过最大审核轮次仍未通过，转人工处理",
            )
        return decision

    # ── 内部收集 ──────────────────────────────────────────

    def _collect_review(self, draft_text: str) -> tuple[bool, list[dict[str, str]]]:
        """调 review_draft（DraftReviewer Service）并归一化为 issue 列表。"""
        raw = self.review_service(draft_text)
        passed = bool(getattr(raw, "passed", True))
        issues: list[dict[str, str]] = []
        for item in getattr(raw, "issues", []) or []:
            if isinstance(item, dict):
                severity = str(item.get("severity", "error"))
                message = str(item.get("message", ""))
                code = str(item.get("code", "unknown"))
                evidence_refs = item.get("evidence_refs", [])
            else:
                severity = str(getattr(item, "severity", "error"))
                message = str(getattr(item, "message", ""))
                code = str(getattr(item, "code", "unknown"))
                evidence_refs = getattr(item, "evidence_refs", [])
            issues.append(
                {
                    "severity": severity,
                    "code": code,
                    "message": message,
                    "ref": ",".join(evidence_refs),
                }
            )
        return passed, issues

    def _collect_grounding(self, draft_text: str) -> dict:
        """调 draft_claim_audit：产品 Claim → Evidence 对齐。"""
        result = self.claim_audit(draft_text) if self.claim_audit else {}
        if not isinstance(result, dict):
            result = {}
        return result

    # ── 判定 ──────────────────────────────────────────────

    def _assess(
        self,
        *,
        passed: bool,
        issues: list[dict[str, Any]],
        grounded_ratio: float,
        unsupported: int,
    ) -> ReviewDecision:
        severity_levels = [i["severity"] for i in issues]
        if any(s in CRITICAL_SEVERITIES for s in severity_levels):
            return ReviewDecision(
                status="BLOCK",
                severity="critical",
                issue_refs=[i["code"] for i in issues],
                reason_summary="存在关键合规/事实问题（critical/error）",
            )
        if unsupported > 0 and grounded_ratio <= BLOCK_MAX_GROUNDED_RATIO:
            return ReviewDecision(
                status="BLOCK",
                severity="error",
                issue_refs=[i["code"] for i in issues],
                revision_instructions=self._grounding_instructions(unsupported),
                reason_summary=f"产品声明缺乏证据支撑（grounded_ratio={grounded_ratio:.2f}）",
            )
        if (not passed) or grounded_ratio < REVISE_MIN_GROUNDED_RATIO or issues:
            instructions = [i["message"] for i in issues if i["message"]]
            if unsupported > 0:
                instructions += self._grounding_instructions(unsupported)
            return ReviewDecision(
                status="REVISE",
                severity="warning",
                issue_refs=[i["code"] for i in issues],
                revision_instructions=instructions or ["请根据审核意见修订稿件"],
                reason_summary=f"需要修改（grounded_ratio={grounded_ratio:.2f}）",
            )
        return ReviewDecision(status="APPROVE", reason_summary="审核通过")

    def _grounding_instructions(self, unsupported: int) -> list[str]:
        return [f"补充或核对 {unsupported} 条产品声明的 source grounding 证据后再发布"]

    async def _external_revise(self, revise: ReviseFn, decision: ReviewDecision, text: str) -> str:
        # 改稿由外部 revise_fn（DraftRevisionSkill）完成；Reviewer 只下发指令。
        return await revise(decision, text)


__all__ = [
    "BLOCK_MAX_GROUNDED_RATIO",
    "CRITICAL_SEVERITIES",
    "MAX_REVIEW_ROUNDS",
    "REVISE_MIN_GROUNDED_RATIO",
    "ReviewDecision",
    "ReviewStatus",
    "ReviewerAgent",
]
