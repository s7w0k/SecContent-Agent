"""RuntimeState / 运行级预算单元测试 — 阶段四 4A Step 4A-1/4A-2。

覆盖：序列化 round-trip、版本迁移、非法状态转换、并发版本冲突、
终态不可逆、预算上限与检查点、审计事件脱敏、from_settings 映射。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.runtime_state import (
    BudgetUsage,
    RunBudget,
    RuntimeState,
    RuntimeStateConflictError,
    RuntimeStateTransitionError,
    TERMINAL_STATUSES,
    apply_state_mutation,
    migrate_runtime_state,
)


def _state(**overrides) -> RuntimeState:
    base = dict(
        run_id="run-1",
        thread_id="thread-1",
        trace_id="trace-1",
        user_id="u1",
        goal="生成一篇 PR 报道",
        acceptance_criteria=["产出一篇中文 PR 报道", "引用至少 2 条证据"],
        budget=RunBudget(max_steps=3, max_tool_calls=5, max_consecutive_failures=2),
    )
    base.update(overrides)
    return RuntimeState(**base)


def _fixed_now() -> datetime:
    return datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


class TestSerialization:
    def test_round_trip_via_migrate(self):
        state = _state()
        raw = state.model_dump(mode="json")
        restored = migrate_runtime_state(raw)
        assert restored == state

    def test_migrate_legacy_version_fills_defaults(self):
        """历史版本（无 schema_version）迁移到 1.0，缺失字段取默认。"""
        legacy = {
            "run_id": "run-legacy",
            "user_id": "u1",
            "goal": "旧版本目标",
            "status": "running",
        }
        migrated = migrate_runtime_state(legacy)
        assert migrated.schema_version == "1.0"
        assert migrated.run_id == "run-legacy"
        assert migrated.acceptance_criteria == []
        assert migrated.budget.max_steps == 20
        assert migrated.status.value == "running"

    def test_unknown_version_rejected(self):
        with pytest.raises(ValueError):
            migrate_runtime_state({"schema_version": "9.9", "run_id": "r"})


class TestTransitions:
    def test_terminal_status_cannot_reenter(self):
        state = _state(status="completed")
        with pytest.raises(RuntimeStateTransitionError):
            state.transition_to("running")

    def test_normal_progression_ok(self):
        state = _state(status="pending")
        next_state, audit = state.transition_to("running", reason="start")
        assert next_state.status.value == "running"
        assert next_state.checkpoint_version == state.checkpoint_version + 1
        assert audit["event_type"] == "state_transition"
        assert audit["from_status"] == "pending"
        assert audit["to_status"] == "running"

    def test_audit_event_is_redacted(self):
        state = _state()
        _, audit = state.transition_to("failed", reason="boom", actor="runtime")
        serialized = str(audit)
        assert "生成一篇 PR 报道" not in serialized  # 不含 goal 全文
        assert "api_key" not in serialized
        assert "prompt" not in serialized
        assert "sequence" in audit and audit["sequence"] >= 1
        assert "reason_code" in audit

    def test_terminal_statuses_all_blocked(self):
        for status in TERMINAL_STATUSES:
            state = _state(status=status.value)
            with pytest.raises(RuntimeStateTransitionError):
                state.transition_to("running")


class TestVersionConflict:
    def test_stale_executor_rejected(self):
        base = _state()
        v1 = base.checkpoint_version

        def _bump(s: RuntimeState) -> RuntimeState:
            return s.model_copy(update={"current_step": "s2"})

        updated = apply_state_mutation(base, expected_version=v1, mutation=_bump)
        assert updated.current_step == "s2"
        assert updated.checkpoint_version == v1 + 1
        # 当前状态已到 v2；旧执行器持 v1 再次覆盖 → 拒绝
        with pytest.raises(RuntimeStateConflictError):
            apply_state_mutation(updated, expected_version=v1, mutation=_bump)

    def test_checkpoint_version_monotonic(self):
        state = _state()
        for _ in range(3):
            state, _ = state.transition_to("running", reason="step")
            if state.status.value == "running":
                # 回到 pending 以便继续迁移（run 合法路径由 Runtime 控制）
                state, _ = state.transition_to("pending", reason="reset")
        assert state.checkpoint_version >= 4


class TestBudget:
    def test_exceeded_returns_limits(self):
        budget = RunBudget(max_steps=2, max_tool_calls=3, max_consecutive_failures=2,
                           max_input_tokens=100, max_output_tokens=100)
        usage = BudgetUsage()
        assert usage.exceeded(budget) == []
        usage.record_step(tokens_in=60, tokens_out=50)
        assert "max_steps" not in usage.exceeded(budget)
        usage.record_step(tokens_in=40, tokens_out=50)
        broken = usage.exceeded(budget)
        assert "max_steps" in broken
        assert "max_input_tokens" in broken
        assert "max_output_tokens" in broken

    def test_deadline_and_runtime_budget(self):
        budget = RunBudget(max_runtime_seconds=10, deadline_at=_fixed_now() + timedelta(seconds=5))
        usage = BudgetUsage(started_at=_fixed_now() - timedelta(seconds=20))
        assert "max_runtime_seconds" in usage.exceeded(budget, now=_fixed_now())
        assert "deadline" in usage.exceeded(budget, now=_fixed_now() + timedelta(seconds=6))

    def test_total_tokens_cap(self):
        budget = RunBudget(max_total_tokens=50)
        usage = BudgetUsage()
        assert usage.can_continue(budget)
        usage.record_tokens(tokens_in=30, tokens_out=20)
        assert "max_total_tokens" in usage.exceeded(budget)

    def test_checkpoint_blocks_next_action_when_at_limit(self):
        budget = RunBudget(max_steps=3, max_tool_calls=3)
        usage = BudgetUsage()
        # 已执行 2 步：仍有第 3 步配额，检查点允许启动
        usage.record_step()
        usage.record_step()
        assert usage.can_continue(budget)
        assert usage.can_start_next_action(budget)
        # 已执行 3 步（== 上限）：宽松检查与检查点都阻止
        usage.record_step()
        assert not usage.can_continue(budget)
        assert not usage.can_start_next_action(budget)

    def test_consecutive_failures_blocks(self):
        budget = RunBudget(max_consecutive_failures=2)
        usage = BudgetUsage()
        usage.record_failure()
        usage.record_failure()
        assert "max_consecutive_failures" in usage.exceeded(budget)
        assert not usage.can_start_next_action(budget)
        usage.record_success()
        assert usage.consecutive_failures == 0

    def test_retry_and_remote_agent_quota(self):
        budget = RunBudget(max_retries=2, remote_agent_quota=2)
        usage = BudgetUsage()
        usage.record_retry()
        usage.record_retry()
        usage.record_remote_agent_call("agent-a")
        usage.record_remote_agent_call("agent-a")
        assert usage.retries == 2
        assert usage.remote_agent_calls["agent-a"] == 2


class TestFromSettings:
    def test_from_settings_maps_budget(self):
        from config import get_settings

        settings = get_settings()
        budget = RunBudget.from_settings(settings)
        assert budget.max_steps == settings.AUTONOMOUS_MAX_STEPS
        assert budget.max_runtime_seconds == settings.AUTONOMOUS_MAX_RUNTIME_SECONDS
        assert budget.max_tool_calls == settings.AUTONOMOUS_MAX_TOOL_CALLS
        assert budget.max_consecutive_failures == settings.AUTONOMOUS_MAX_CONSECUTIVE_FAILURES


class TestSensitiveDataAbsent:
    def test_state_never_contains_secrets(self):
        """状态字段集中不应出现密钥/提示词字段名。"""
        state = _state()
        raw = state.model_dump()
        assert "api_key" not in raw
        assert "prompt" not in raw
        assert "thinking" not in raw
        # decision_summaries 只保存摘要哈希
        state2 = _state()
        assert all(not s.get("reason") or len(s.get("reason", "")) <= 200
                   for s in state2.model_dump()["decision_summaries"])
