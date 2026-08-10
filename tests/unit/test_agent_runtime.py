"""AgentRuntime 主循环单元测试 — 阶段四 4A Step 4A-6。

覆盖状态机各分支：LOAD/PLAN/POLICY_CHECK/EXECUTE/OBSERVE/VALIDATE/CHECKPOINT、
目标完成、无可执行步骤、人工审批（WAIT_APPROVAL + 恢复）、策略熔断、
预算耗尽、连续失败、循环检测、用户取消、租约丢失、
幂等重放跳过、指数退避重试、检查点/账本写入、敏感信息不落日志。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from agent.agent_runtime import AgentRuntime, PlannedAction
from agent.goal_validator import GoalValidator
from agent.model_router import ModelCapability, ModelRouter, SensitivityLevel
from agent.runtime_state import BudgetUsage, RunBudget, RuntimeState, RuntimeStatus

FIXED_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


def _state(**overrides) -> RuntimeState:
    base = {
        "run_id": "run-1",
        "user_id": "u1",
        "goal": "完成一个测试目标",
        "acceptance_criteria": ["完成一个动作"],
        "budget": RunBudget(max_steps=10, max_consecutive_failures=2),
        # started_at 对齐 FIXED_NOW，避免真实时间偏差导致运行时预算误判
        "usage": BudgetUsage(started_at=FIXED_NOW, last_action_at=FIXED_NOW),
    }
    base.update(overrides)
    return RuntimeState(**base)


async def _no_sleep(_delay: float) -> None:
    return None


def _make_planner(actions: list[PlannedAction | None]):
    it = iter(actions)

    async def _plan(state: RuntimeState) -> PlannedAction | None:
        try:
            return next(it)
        except StopIteration:
            return None

    return _plan


async def _ok_executor(state, action, meta):
    """成功且产生证据（使验收条件满足）。"""
    return {"ok": True, "evidence": [{"acceptance_index": 0}], "duration_ms": 1}


async def _ok_no_evidence(state, action, meta):
    return {"ok": True, "duration_ms": 1}


def _runtime(*, planner, executor, **kw) -> AgentRuntime:
    base = {
        "planner": planner,
        "executor": executor,
        "goal_validator": GoalValidator(
            required_artifact_keys=(), high_risk_requires_confirm=False
        ),
        "sleep": _no_sleep,
        "backoff_jitter": 0.0,
    }
    base.update(kw)
    return AgentRuntime(**base)


class TestStateMachine:
    async def test_completes_when_goal_met(self):
        planner = _make_planner([PlannedAction(step_id="s1", tool_name="retrieve_articles")])
        runtime = _runtime(planner=planner, executor=_ok_executor)
        res = await runtime.run(_state(), now=FIXED_NOW)
        assert res.status == RuntimeStatus.COMPLETED
        assert res.reason_code == "goal_complete"
        assert res.completed_steps == ["s1"]
        assert res.evidence_count == 1
        assert "load" in res.phases and "execute" in res.phases and "validate" in res.phases

    async def test_stops_when_planner_returns_none(self):
        async def _empty_planner(state):
            return None

        runtime = _runtime(planner=_empty_planner, executor=_ok_no_evidence)
        res = await runtime.run(_state(), now=FIXED_NOW)
        assert res.status == RuntimeStatus.STOPPED
        assert res.reason_code == "no_executable_steps"

    async def test_policy_breaker_stops_on_l3(self):
        planner = _make_planner(
            [PlannedAction(step_id="s1", tool_name="delete_article", args={"article_id": "a1"})]
        )
        runtime = _runtime(planner=planner, executor=_ok_no_evidence)
        res = await runtime.run(_state(), now=FIXED_NOW)
        assert res.status == RuntimeStatus.STOPPED
        assert res.reason_code == "policy_breaker"

    async def test_budget_exceeded_stops(self):
        state = _state(budget=RunBudget(max_steps=1, max_consecutive_failures=3))
        planner = _make_planner([PlannedAction(step_id="s1", tool_name="retrieve_articles")])
        runtime = _runtime(planner=planner, executor=_ok_no_evidence)
        res = await runtime.run(state, now=FIXED_NOW)
        assert res.status == RuntimeStatus.BUDGET_EXCEEDED
        assert res.reason_code == "budget_exceeded"

    async def test_consecutive_failures_stops(self):
        async def _fail_executor(state, action, meta):
            return {"ok": False, "error": "boom", "error_code": "boom"}

        planner = _make_planner(
            [
                PlannedAction(step_id="s1", tool_name="retrieve_articles"),
                PlannedAction(step_id="s2", tool_name="retrieve_articles"),
            ]
        )
        runtime = _runtime(planner=planner, executor=_fail_executor)
        res = await runtime.run(_state(), now=FIXED_NOW)
        assert res.status == RuntimeStatus.FAILED
        assert res.reason_code == "consecutive_failures"
        assert res.failed_steps == ["s1", "s2"]

    async def test_loop_detected_stops(self):
        planner = _make_planner(
            [
                PlannedAction(step_id="s1", tool_name="retrieve_articles"),
                PlannedAction(step_id="s1", tool_name="retrieve_articles"),
            ]
        )
        runtime = _runtime(planner=planner, executor=_ok_no_evidence)
        res = await runtime.run(_state(), now=FIXED_NOW)
        assert res.status == RuntimeStatus.STOPPED
        assert res.reason_code == "loop_detected"

    async def test_user_cancel_stops(self):
        cancel_event = asyncio.Event()
        cancel_event.set()
        planner = _make_planner([PlannedAction(step_id="s1", tool_name="retrieve_articles")])
        runtime = _runtime(planner=planner, executor=_ok_executor)
        res = await runtime.run(_state(), now=FIXED_NOW, cancel_event=cancel_event)
        assert res.status == RuntimeStatus.CANCELED
        assert res.reason_code == "user_canceled"
        assert res.rounds == 0

    async def test_lease_lost_stops_immediately(self):
        planner = _make_planner([PlannedAction(step_id="s1", tool_name="retrieve_articles")])
        runtime = _runtime(planner=planner, executor=_ok_executor)
        res = await runtime.run(_state(), now=FIXED_NOW, lease_lost=True)
        assert res.status == RuntimeStatus.STOPPED
        assert res.reason_code == "lease_lost"
        assert res.rounds == 0


class TestApproval:
    async def test_waits_approval_on_l2(self):
        planner = _make_planner(
            [
                PlannedAction(
                    step_id="s1",
                    tool_name="submit_pr",
                    args={"title": "t", "body": "b", "idempotency_key": "ik-1"},
                )
            ]
        )
        runtime = _runtime(planner=planner, executor=_ok_no_evidence)
        res = await runtime.run(_state(), now=FIXED_NOW)
        assert res.status == RuntimeStatus.WAITING_APPROVAL
        assert res.reason_code == "waiting_approval"
        assert res.final_state is not None
        assert len(res.final_state.approval_state.pending_approvals) == 1
        pending = res.final_state.approval_state.pending_approvals[0]
        assert pending.action == "submit_pr"
        assert pending.risk_level == "L2"

    async def test_resume_after_approval(self):
        planner = _make_planner(
            [
                PlannedAction(
                    step_id="s1",
                    tool_name="submit_pr",
                    args={"title": "t", "body": "b", "idempotency_key": "ik-1"},
                )
            ]
        )
        runtime = _runtime(planner=planner, executor=_ok_no_evidence)
        res = await runtime.run(_state(), now=FIXED_NOW)
        assert res.status == RuntimeStatus.WAITING_APPROVAL
        mid = res.final_state

        # 人工审批通过：清空待审批，注入一次性授权
        approved = mid.model_copy(
            update={
                "approval_state": mid.approval_state.model_copy(
                    update={"pending_approvals": [], "approved_tokens": ["tok-1"]}
                )
            }
        )
        planner2 = _make_planner([PlannedAction(step_id="s2", tool_name="retrieve_articles")])
        runtime2 = _runtime(planner=planner2, executor=_ok_executor)
        res2 = await runtime2.run(approved, now=FIXED_NOW)
        assert res2.status == RuntimeStatus.COMPLETED
        assert res2.completed_steps == ["s2"]


class TestResilience:
    async def test_idempotent_replay_skipped(self):
        calls: list[str] = []

        async def _counting_executor(state, action, meta):
            calls.append(action.step_id)
            return {"ok": True, "duration_ms": 1}

        planner = _make_planner(
            [
                PlannedAction(
                    step_id="s1",
                    tool_name="crawl_overseas_news",
                    args={"days": 1, "idempotency_key": "ik-1"},
                ),
                PlannedAction(
                    step_id="s2",
                    tool_name="crawl_overseas_news",
                    args={"days": 1, "idempotency_key": "ik-1"},
                ),
            ]
        )
        runtime = _runtime(planner=planner, executor=_counting_executor)
        res = await runtime.run(_state(), now=FIXED_NOW)
        # 相同 idempotency_key 的副作用只执行一次，不盲目重放
        assert calls == ["s1"]
        assert res.completed_steps == ["s1"]
        assert res.status == RuntimeStatus.STOPPED  # 无更多步骤

    async def test_retry_with_backoff_succeeds(self):
        calls = {"n": 0}

        async def _flaky_executor(state, action, meta):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "ok": False,
                    "error": "transient",
                    "error_code": "timeout",
                    "retryable": True,
                }
            return {"ok": True, "evidence": [{"acceptance_index": 0}], "duration_ms": 1}

        planner = _make_planner([PlannedAction(step_id="s1", tool_name="retrieve_articles")])
        runtime = _runtime(planner=planner, executor=_flaky_executor, max_retries=2)
        res = await runtime.run(_state(), now=FIXED_NOW)
        assert res.status == RuntimeStatus.COMPLETED
        assert res.budget_usage["retries"] == 1
        assert calls["n"] == 2

    async def test_non_retryable_error_no_retry(self):
        calls = {"n": 0}

        async def _fail_hard(state, action, meta):
            calls["n"] += 1
            return {
                "ok": False,
                "error": "permanent",
                "error_code": "bad_input",
                "retryable": False,
            }

        planner = _make_planner([PlannedAction(step_id="s1", tool_name="retrieve_articles")])
        runtime = _runtime(planner=planner, executor=_fail_hard, max_retries=3)
        res = await runtime.run(_state(), now=FIXED_NOW)
        assert res.status == RuntimeStatus.FAILED
        assert calls["n"] == 1
        assert res.budget_usage["retries"] == 0

    async def test_checkpoint_and_ledger_written(self):
        checkpoints: list[int] = []
        ledgers: list[str] = []

        async def _ckpt(state):
            checkpoints.append(state.checkpoint_version)

        async def _ledger(state, action, meta):
            ledgers.append(action.step_id)

        planner = _make_planner(
            [
                PlannedAction(
                    step_id="s1",
                    tool_name="crawl_overseas_news",
                    args={"days": 1, "idempotency_key": "ik-1"},
                )
            ]
        )
        runtime = _runtime(
            planner=planner, executor=_ok_no_evidence, checkpointer=_ckpt, ledger=_ledger
        )
        await runtime.run(_state(), now=FIXED_NOW)
        assert checkpoints  # 每轮至少写一次检查点
        assert ledgers == ["s1"]  # 有副作用的步骤写入账本


class TestSecurity:
    async def test_sensitive_args_not_persisted(self):
        planner = _make_planner(
            [
                PlannedAction(
                    step_id="s1", tool_name="retrieve_articles", args={"api_key": "sk-secret-123"}
                )
            ]
        )
        runtime = _runtime(planner=planner, executor=_ok_no_evidence)
        res = await runtime.run(_state(), now=FIXED_NOW)
        # 非法参数 → 策略拒绝 → 熔断停止
        assert res.status == RuntimeStatus.STOPPED
        assert res.reason_code == "policy_breaker"
        raw = json.dumps(
            [s.model_dump(mode="json") for s in res.final_state.decision_summaries],
            ensure_ascii=False,
        )
        assert "sk-secret-123" not in raw
        # 决策摘要只保存哈希/原因码，不含参数原文
        for summary in res.final_state.decision_summaries:
            assert "api_key" not in summary.action
            assert "sk-secret" not in summary.reason

    async def test_model_route_recorded_without_sensitive_content(self):
        router = ModelRouter(
            [
                ModelCapability(
                    name="deepseek-chat",
                    max_sensitivity=SensitivityLevel.L1,
                    max_context_chars=12000,
                ),
                ModelCapability(
                    name="cheap-lite", max_sensitivity=SensitivityLevel.L0, max_context_chars=8000
                ),
            ],
            default_model="deepseek-chat",
            fallback_chain=("cheap-lite",),
        )
        planner = _make_planner([PlannedAction(step_id="s1", tool_name="retrieve_articles")])
        runtime = _runtime(planner=planner, executor=_ok_executor, model_router=router)
        res = await runtime.run(_state(), now=FIXED_NOW)
        assert res.status == RuntimeStatus.COMPLETED
        route_decisions = [
            s for s in res.final_state.decision_summaries if s.action == "model_route"
        ]
        assert route_decisions
        assert "deepseek-chat" in route_decisions[0].reason
        assert route_decisions[0].outcome == "ok"
