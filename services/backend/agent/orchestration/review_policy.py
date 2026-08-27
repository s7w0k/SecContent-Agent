"""DraftReviewPolicy - Reviewer 进入 Draft 主链的 Orchestrator Hook（Final Closure 计划 EPIC-B §19-28）。

定位：
  - Reviewer 保持 Specialist Agent，不作为普通 Skill（§20）——独立判断/拒绝/返工/循环。
  - 只在 Draft 步骤完成后被 Orchestrator 触发（§21 / §52），且绝不允许 Reviewer 直接改稿（§28）。
  - REVISE → 派发 DraftRevisionSkill（§25 / §28）；BLOCK → Orchestrator status=BLOCKED（§26）；
    轮次受 budget.max_review_rounds 限制，超限 BLOCK/转人工（§27）。
"""

from __future__ import annotations

from typing import Any

from agent.orchestration.contracts import OrchestratorState, SkillPlan
from agent.skills.contracts import SkillRequest, SkillResult
from agent.specialists.reviewer_agent import ReviewerAgent

DEFAULT_REVIEW_SKILLS: frozenset[str] = frozenset({"draft-writing", "draft-revision"})


def _parse_ref(ref: str) -> tuple[str, str, int] | None:
    """解析 "DraftArtifact:<id>@<version>" → (type, id, version)。"""
    if ":" not in ref or "@" not in ref:
        return None
    type_part, version_part = ref.rsplit("@", 1)
    artifact_id = type_part.split(":", 1)[1]
    try:
        version = int(version_part)
    except ValueError:
        return None
    return type_part.split(":", 1)[0], artifact_id, version


class DraftReviewPolicy:
    """在 Draft 步骤完成后执行审查，必要时派发修订并再审（有界循环）。"""

    def __init__(
        self,
        reviewer_agent: ReviewerAgent,
        artifact_store: Any,
        *,
        review_skills: frozenset[str] = DEFAULT_REVIEW_SKILLS,
    ) -> None:
        self.reviewer_agent = reviewer_agent
        self.artifact_store = artifact_store
        self.review_skills = frozenset(review_skills)
        self.revision_requests: list[dict[str, Any]] = []

    async def after_draft(
        self,
        *,
        plan: SkillPlan,
        state: OrchestratorState,
        step: Any,
        skill_runtime: Any,
        scopes: frozenset[str] | set[str] | None,
        user_id: str,
        tenant_id: str,
        trace_id: str,
        task_id: str,
        budget: Any | None = None,
    ) -> None:
        """审查刚完成的 Draft 步骤（只读，不直接改稿 §28）。"""
        if step.skill_name not in self.review_skills:
            return
        draft_ref = state.artifact_refs.get(step.step_id)
        if not draft_ref:
            return

        max_rounds = max(1, int(getattr(budget, "max_review_rounds", 2)))
        text = await self._draft_text(draft_ref)
        decision = await self.reviewer_agent.review(draft_text=text)
        state.reviewer_rounds = max(state.reviewer_rounds, 1)
        self.revision_requests.append(
            {
                "step_id": step.step_id,
                "round": state.reviewer_rounds,
                "decision": decision.status,
            }
        )

        if decision.status == "APPROVE":
            return
        if decision.status == "BLOCK":
            state.status = "BLOCKED"
            return

        # REVISE → 派发 DraftRevisionSkill → 再审（有界 §27）
        while decision.status == "REVISE":
            if state.reviewer_rounds >= max_rounds:
                state.status = "BLOCKED"
                return
            parent_ref = state.artifact_refs.get(step.step_id) or draft_ref
            source = await self._source_artifact(parent_ref)
            instruction = "；".join(decision.revision_instructions or ["请根据审核意见修订稿件"])
            request = SkillRequest(
                skill_name="draft-revision",
                run_id=state.run_id,
                user_id=user_id,
                tenant_id=tenant_id,
                trace_id=trace_id or state.run_id,
                input_refs={
                    "parent_artifact_ref": parent_ref,
                    "revision_instruction": instruction,
                },
                params={
                    "source_artifact": source,
                    "instruction": instruction,
                    "expected_version": int(source.get("version", 1)) + 1,
                    "idempotency_key": (
                        f"{task_id}-{plan.plan_id}-{step.step_id}-rev{state.reviewer_rounds}"
                    ),
                },
            )
            rev = await skill_runtime.execute(request, scopes=scopes)
            if rev.status not in ("SUCCEEDED", "PARTIAL"):
                state.status = "FAILED"
                return
            if rev.artifact_refs:
                state.artifact_refs[step.step_id] = rev.artifact_refs[0]
                text = await self._draft_text(rev.artifact_refs[0])
            state.reviewer_rounds += 1
            decision = await self.reviewer_agent.review(draft_text=text)

        if decision.status == "BLOCK":
            state.status = "BLOCKED"

    async def _draft_text(self, ref: str) -> str:
        parsed = _parse_ref(ref)
        if parsed is None:
            return ""
        art_type, artifact_id, version = parsed
        try:
            payload = await self.artifact_store.get(
                artifact_id=artifact_id, artifact_type=art_type, version=version
            )
        except Exception:
            return ""
        return str(payload.get("content") or "")

    async def _source_artifact(self, ref: str) -> dict[str, Any]:
        parsed = _parse_ref(ref)
        if parsed is None:
            return {"artifact_id": ref, "version": 1, "content_hash": ""}
        art_type, artifact_id, version = parsed
        try:
            payload = await self.artifact_store.get(
                artifact_id=artifact_id, artifact_type=art_type, version=version
            )
        except Exception:
            payload = {}
        return {
            "artifact_id": artifact_id,
            "version": version,
            "content_hash": str(payload.get("content_hash") or ""),
        }


class StubReviewSkillResult:
    """测试/缺省用的轻量 SkillResult 构造。"""

    @staticmethod
    def ok(ref: str) -> SkillResult:
        return SkillResult.succeeded("draft-revision", artifact_refs=[ref])


__all__ = [
    "DEFAULT_REVIEW_SKILLS",
    "DraftReviewPolicy",
    "StubReviewSkillResult",
]
