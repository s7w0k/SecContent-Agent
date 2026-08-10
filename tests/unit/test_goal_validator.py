"""GoalValidator / 循环检测 / 终止条件单元测试 — 阶段四 4A Step 4A-4。

覆盖：产物结构、规则校验、验收条件证据映射、未决审批/失败步骤/预算风险、
高风险人工确认、循环检测四种组合、强制停止条件全集合。
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.goal_validator import (
    CheckStatus,
    GoalValidator,
    LoopDetector,
    LoopSignal,
    decide_termination,
)
from agent.runtime_state import (
    BudgetUsage,
    DecisionSummary,
    EvidenceRecord,
    PendingApproval,
    RunBudget,
    RuntimeState,
    RuntimeStatus,
)


def _fixed_now() -> datetime:
    return datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


def _state(**overrides) -> RuntimeState:
    base = {
        "run_id": "run-1",
        "user_id": "u1",
        "goal": "完成一篇 PR 报道",
        "acceptance_criteria": ["产出一篇中文 PR 报道", "引用至少 2 条证据"],
        "budget": RunBudget(max_steps=5, max_consecutive_failures=2),
    }
    base.update(overrides)
    return RuntimeState(**base)


def _evidence(state: RuntimeState, index: int, eid: str = "ev-1") -> RuntimeState:
    return state.model_copy(
        update={
            "evidence": [
                *state.evidence,
                EvidenceRecord(evidence_id=eid, acceptance_index=index, kind="tool_result"),
            ]
        }
    )


class TestGoalValidator:
    def test_missing_artifact_incomplete(self):
        validator = GoalValidator()
        result = validator.validate(_state(), artifacts={})
        assert result.status == "incomplete"
        assert not result.structure_ok

    def test_rule_check_failure(self):
        def _must_contain_keyword(artifacts):
            return "" if "中文" in str(artifacts.get("report")) else "report missing 中文"

        validator = GoalValidator(rule_checks=(_must_contain_keyword,))
        result = validator.validate(_state(), artifacts={"report": "English only"})
        assert result.status == "incomplete"
        assert not result.rules_ok

    def test_evidence_mapping_partial(self):
        validator = GoalValidator()
        state = _evidence(_state(), index=0, eid="ev-1")
        result = validator.validate(state, artifacts={"report": "r"})
        assert result.status == "evidence_missing"
        assert result.checks[0].status == CheckStatus.SATISFIED
        assert result.checks[1].status == CheckStatus.MISSING_EVIDENCE
        assert result.satisfied_count == 1
        assert result.total_criteria == 2

    def test_all_criteria_met_complete(self):
        validator = GoalValidator(high_risk_requires_confirm=False)
        state = _state()
        state = _evidence(state, 0, "ev-1")
        state = _evidence(state, 1, "ev-2")
        result = validator.validate(state, artifacts={"report": "r"})
        assert result.status == "complete"

    def test_pending_approval_blocks(self):
        validator = GoalValidator()
        state = _state()
        state = _evidence(state, 0, "ev-1")
        state = _evidence(state, 1, "ev-2")
        state = state.model_copy(
            update={
                "approval_state": state.approval_state.model_copy(
                    update={
                        "pending_approvals": [
                            PendingApproval(approval_id="ap-1", action="submit_pr", risk_level="L2")
                        ]
                    }
                )
            }
        )
        result = validator.validate(state, artifacts={"report": "r"})
        assert result.status == "blocked"
        assert result.pending_approvals == 1

    def test_budget_risk_blocks(self):
        validator = GoalValidator()
        state = _state()
        state = _evidence(state, 0, "ev-1")
        state = _evidence(state, 1, "ev-2")
        usage = BudgetUsage()
        usage.record_step()  # 构造未超限
        budget = RunBudget(max_steps=0)  # 0 上限 → 立即超限
        state = state.model_copy(update={"budget": budget})
        result = validator.validate(state, artifacts={"report": "r"})
        assert result.status == "blocked"
        assert result.budget_risk

    def test_high_risk_requires_human_confirm(self):
        validator = GoalValidator(high_risk_requires_confirm=True)
        state = _state()
        state = _evidence(state, 0, "ev-1")
        state = _evidence(state, 1, "ev-2")
        result = validator.validate(state, artifacts={"report": "r"})
        assert result.status == "needs_human_confirm"
        # 有人工确认 token → complete
        state2 = state.model_copy(
            update={
                "approval_state": state.approval_state.model_copy(
                    update={"approved_tokens": ["final-ok"]}
                )
            }
        )
        result2 = validator.validate(state2, artifacts={"report": "r"})
        assert result2.status == "complete"


class TestLoopDetector:
    @staticmethod
    def _push(
        state: RuntimeState,
        step_id: str,
        *,
        tool: str = "t",
        args_hash: str = "h",
        outcome: str = "success",
        reason: str = "",
    ) -> RuntimeState:
        return state.model_copy(
            update={
                "decision_summaries": [
                    *state.decision_summaries,
                    DecisionSummary(
                        step_id=step_id,
                        phase="execute",
                        action="run",
                        tool_name=tool,
                        args_hash=args_hash,
                        outcome=outcome,
                        reason=reason,
                    ),
                ]
            }
        )

    def test_identical_plan_repeated(self):
        state = _state()
        state = self._push(state, "s1")
        state = self._push(state, "s1")
        signal = LoopDetector().detect_loop(state)
        assert signal.detected
        assert signal.kind == "identical_plan"

    def test_similar_recent_actions(self):
        state = _state()
        detector = LoopDetector(window=4, similarity_threshold=0.9)
        # 4 步全部共享同一指纹 → 相似度 4/4 = 1.0 ≥ 0.9 → 循环
        state = self._push(state, "s1", args_hash="a")
        state = self._push(state, "s2", args_hash="a")
        state = self._push(state, "s3", args_hash="a")
        state = self._push(state, "s4", args_hash="a")
        signal = detector.detect_loop(state)
        assert signal.detected
        assert signal.kind == "similar_recent"

    def test_repeated_error_signature(self):
        state = _state()
        # 有证据避免 no_new_evidence 分支先命中；不同参数指纹避免 similar_recent 分支命中
        state = state.model_copy(
            update={
                "evidence": [
                    *state.evidence,
                    EvidenceRecord(evidence_id="ev-1", acceptance_index=0, kind="tool_result"),
                ]
            }
        )
        detector = LoopDetector(repeated_error_limit=3)
        state = self._push(
            state, "s1", tool="fetch", args_hash="h1", outcome="failed", reason="timeout"
        )
        state = self._push(
            state, "s2", tool="fetch", args_hash="h2", outcome="failed", reason="timeout"
        )
        state = self._push(
            state, "s3", tool="fetch", args_hash="h3", outcome="failed", reason="timeout"
        )
        signal = detector.detect_loop(state)
        assert signal.detected
        assert signal.kind == "repeated_error"

    def test_no_new_evidence(self):
        state = _state()
        detector = LoopDetector(no_evidence_limit=3)
        # 不同参数指纹避免 similar_recent 分支先命中，验证 no_new_evidence 分支
        state = self._push(state, "s1", args_hash="h1", outcome="skipped", reason="blocked")
        state = self._push(state, "s2", args_hash="h2", outcome="skipped", reason="blocked")
        state = self._push(state, "s3", args_hash="h3", outcome="skipped", reason="blocked")
        signal = detector.detect_loop(state)
        assert signal.detected
        assert signal.kind == "no_new_evidence"

    def test_no_loop_for_progressing_run(self):
        state = _state()
        state = self._push(state, "s1", args_hash="a")
        state = state.model_copy(
            update={
                "evidence": [
                    *state.evidence,
                    EvidenceRecord(evidence_id="ev-1", acceptance_index=0, kind="tool_result"),
                ]
            }
        )
        state = self._push(state, "s2", args_hash="b")
        signal = LoopDetector().detect_loop(state)
        assert not signal.detected


class TestTermination:
    def test_goal_complete(self):
        from agent.goal_validator import GoalValidationResult

        state = _state()
        decision = decide_termination(
            state,
            goal_result=GoalValidationResult(status="complete"),
        )
        assert decision.stop
        assert decision.status == RuntimeStatus.COMPLETED

    def test_budget_exceeded(self):
        state = _state()
        budget = RunBudget(max_steps=1)
        usage = BudgetUsage()
        usage.record_step()
        state = state.model_copy(update={"budget": budget, "usage": usage})
        decision = decide_termination(state, now=_fixed_now())
        assert decision.stop
        assert decision.status == RuntimeStatus.BUDGET_EXCEEDED

    def test_user_cancel_priority(self):
        state = _state()
        decision = decide_termination(state, user_canceled=True, now=_fixed_now())
        assert decision.status == RuntimeStatus.CANCELED

    def test_lease_lost_stops(self):
        state = _state()
        decision = decide_termination(state, lease_lost=True)
        assert decision.status == RuntimeStatus.STOPPED

    def test_policy_breaker_stops(self):
        state = _state()
        decision = decide_termination(state, policy_breaker=True)
        assert decision.status == RuntimeStatus.STOPPED

    def test_loop_detected_stops(self):
        state = _state()
        decision = decide_termination(
            state, loop_signal=LoopSignal(detected=True, kind="identical_plan")
        )
        assert decision.stop
        assert decision.reason_code == "loop_detected"

    def test_consecutive_failures_fails(self):
        state = _state()
        usage = BudgetUsage()
        usage.record_failure()
        usage.record_failure()
        state = state.model_copy(update={"usage": usage})
        decision = decide_termination(state)
        assert decision.status == RuntimeStatus.FAILED

    def test_no_executable_steps(self):
        state = _state()
        decision = decide_termination(state, no_executable_steps=True)
        assert decision.status == RuntimeStatus.STOPPED

    def test_continues_when_ok(self):
        state = _state()
        # started_at 对齐固定 now，避免真实时间与 _fixed_now 偏差导致误判预算超限
        state = state.model_copy(update={"usage": BudgetUsage(started_at=_fixed_now())})
        decision = decide_termination(state, now=_fixed_now())
        assert not decision.stop
