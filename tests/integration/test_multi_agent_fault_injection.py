"""MultiAgent 故障注入集成测试 — 阶段三 Step 10。

覆盖：
  - 每个 Worker 的 timeout / 非重试异常 / 重试后成功；
  - 租约接管与迟到写拒绝（fencing 递增）；
  - 取消竞态（cancel_event / deadline）；
  - 重复触发（init_run 幂等、(run_id, step_id) 唯一）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from agent.execution_step_ledger import LeaseConflictError
from agent.orchestrator import build_waves
from agent.worker_registry import WorkerResult

from tests.integration._multi_agent_helpers import (
    DEFAULT_WORKERS,
    adapter_by_name,
    default_plan,
    fixed_now,
    make_db,
    make_execution_stack,
    make_registry,
    two_wave_plan,
)


class TestWorkerFaults:
    @pytest.mark.parametrize("worker", DEFAULT_WORKERS)
    async def test_worker_timeout_becomes_dead_lettered_partial(self, worker):
        """每个 Worker 挂起超时 → 重试耗尽 → dead_lettered；optional 步骤按策略跳过。"""
        db = make_db()
        registry = make_registry(
            overrides={worker: {"hang": True, "timeout_s": 1, "max_attempts": 1}}
        )
        _, ledger, orchestrator = make_execution_stack(db, registry)
        plan = default_plan(run_id=f"run-timeout-{worker}")
        await ledger.init_run(plan)
        outcome = await orchestrator.run(plan, user_id="u1")

        step_spec = next(s for s in plan.steps if s.worker == worker)
        by_step = {s.step_id: s for s in outcome.steps}
        target = next(s for s in by_step.values() if s.worker == worker)
        if step_spec.policy == "required":
            # 故障 Worker 重试耗尽死信；run 终态 partial（保留已确认产物）
            assert target.status == "dead_lettered"
            assert target.error_type in ("timeout", "TimeoutError")
            assert outcome.status == "partial"
            entry = await ledger.get_step(f"run-timeout-{worker}", target.step_id)
            assert entry.status == "dead_lettered"
            assert entry.retryable is True
        else:
            # optional/best_effort 步骤失败按策略跳过，不阻断依赖
            assert target.status == "skipped"
            assert outcome.status == "completed"

    @pytest.mark.parametrize("worker", DEFAULT_WORKERS)
    async def test_worker_non_retryable_failure_fails_run(self, worker):
        """每个 Worker 非重试异常 → failed；optional 步骤按策略跳过。"""
        db = make_db()
        registry = make_registry(overrides={worker: {"status": "failed", "retryable": False}})
        _, ledger, orchestrator = make_execution_stack(db, registry)
        plan = default_plan(run_id=f"run-fail-{worker}")
        await ledger.init_run(plan)
        outcome = await orchestrator.run(plan, user_id="u1")

        step_spec = next(s for s in plan.steps if s.worker == worker)
        by_step = {s.step_id: s for s in outcome.steps}
        target = next(s for s in by_step.values() if s.worker == worker)
        if step_spec.policy == "required":
            assert target.status == "failed"
            assert outcome.status == "failed"
        else:
            assert target.status == "skipped"
            assert outcome.status == "completed"

    async def test_retry_then_success(self):
        """首个 Worker 前 2 次失败、第 3 次成功 → succeeded，attempt/fencing 递增。"""
        db = make_db()
        registry = make_registry(overrides={"crawl": {"attempts_until_success": 3}})
        _, ledger, orchestrator = make_execution_stack(db, registry, max_attempts=3)
        plan = default_plan(run_id="run-retry")
        await ledger.init_run(plan)
        outcome = await orchestrator.run(plan, user_id="u1")

        assert outcome.status == "completed"
        crawl = adapter_by_name(registry, "crawl")
        assert len(crawl.executions) == 3
        by_step = {s.step_id: s for s in outcome.steps}
        assert by_step["s1_crawl"].attempt == 3
        entry = await ledger.get_step("run-retry", "s1_crawl")
        assert entry.status == "succeeded"
        assert entry.fencing_token == 3


class TestLeaseFencing:
    async def test_stale_write_rejected_after_takeover(self):
        """租约接管后旧 owner 迟到写因 fencing 不匹配被拒，新 owner 可提交。"""
        db = make_db()
        registry = make_registry()
        _, ledger, _ = make_execution_stack(db, registry)
        plan = default_plan(run_id="run-fence")
        await ledger.init_run(plan)

        old = await ledger.begin_attempt(
            run_id="run-fence",
            step_id="s1_crawl",
            owner_id="old",
            attempt=1,
            now=fixed_now() - timedelta(seconds=300),
        )
        # 接管（模拟 recoverer 发现租约过期）
        taken = await ledger._takeover_one(
            "run-fence", "s1_crawl", "new", old.fencing_token + 1, fixed_now()
        )
        assert taken.fencing_token == 2
        # 旧 owner 迟到写被拒
        stale = WorkerResult(step_id="s1_crawl", worker="crawl", status="succeeded", attempt=1)
        with pytest.raises(LeaseConflictError):
            await ledger.complete(
                run_id="run-fence",
                step_id="s1_crawl",
                owner_id="old",
                fencing_token=1,
                result=stale,
            )
        # 新 owner 提交成功
        fresh = WorkerResult(
            step_id="s1_crawl",
            worker="crawl",
            status="succeeded",
            attempt=2,
            idempotency_key="k",
            input_hash="h",
            result_hash="r",
        )
        entry = await ledger.complete(
            run_id="run-fence",
            step_id="s1_crawl",
            owner_id="new",
            fencing_token=2,
            result=fresh,
        )
        assert entry.status == "succeeded"

    async def test_late_complete_after_terminal_rejected(self):
        """步骤进入终态后再次 complete 被 CAS 拒绝。"""
        db = make_db()
        registry = make_registry()
        _, ledger, _ = make_execution_stack(db, registry)
        plan = default_plan(run_id="run-late")
        await ledger.init_run(plan)

        claim = await ledger.begin_attempt(
            run_id="run-late", step_id="s1_crawl", owner_id="o", attempt=1
        )
        ok = WorkerResult(step_id="s1_crawl", worker="crawl", status="succeeded", attempt=1)
        await ledger.complete(
            run_id="run-late",
            step_id="s1_crawl",
            owner_id="o",
            fencing_token=claim.fencing_token,
            result=ok,
        )
        with pytest.raises(LeaseConflictError):
            await ledger.complete(
                run_id="run-late",
                step_id="s1_crawl",
                owner_id="o",
                fencing_token=claim.fencing_token,
                result=ok,
            )


class TestCancelRace:
    async def test_cancel_event_stops_later_waves(self):
        """首波执行中置位 cancel_event → 后续波不调度，run canceled。"""
        db = make_db()
        registry = make_registry()
        _, ledger, orchestrator = make_execution_stack(db, registry)
        plan = two_wave_plan()
        await ledger.init_run(plan)
        assert len(build_waves(plan)) == 2

        cancel_event = asyncio.Event()
        crawl = adapter_by_name(registry, "crawl")
        crawl._on_execute = lambda state, ctx, lease: cancel_event.set()

        outcome = await orchestrator.run(plan, cancel_event=cancel_event)
        assert outcome.status == "canceled"
        by_step = {s.step_id: s for s in outcome.steps}
        assert by_step["s1_a"].status == "succeeded"
        assert by_step["s2_b"].status == "canceled"

    async def test_deadline_cancels_immediately(self):
        """deadline 已过期 → 立即取消，无任何步骤执行。"""
        db = make_db()
        registry = make_registry()
        _, ledger, orchestrator = make_execution_stack(db, registry)
        plan = default_plan(run_id="run-deadline")
        await ledger.init_run(plan)
        outcome = await orchestrator.run(plan, deadline_at=time.monotonic() - 1)
        assert outcome.status == "canceled"
        assert all(s.status == "canceled" for s in outcome.steps)
        # 没有任何 Worker 被执行
        for name in registry.names():
            assert adapter_by_name(registry, name).executions == []


class TestDuplicateTriggers:
    async def test_init_run_idempotent_no_duplicate_entries(self):
        """重复触发 init_run 幂等；(run_id, step_id) 唯一。"""
        db = make_db()
        registry = make_registry()
        _, ledger, _ = make_execution_stack(db, registry)
        plan = default_plan(run_id="run-dup")
        first = await ledger.init_run(plan)
        second = await ledger.init_run(plan)
        assert len(first) == 8
        assert second == []
        col = db["execution_step_ledger"]
        ids = [(d["run_id"], d["step_id"]) for d in col.docs if d["run_id"] == "run-dup"]
        assert len(ids) == len(set(ids)) == 8

    async def test_completed_run_rerun_produces_no_business_writes(self):
        """已完成 run 重复执行：步骤已是终态，领不到租约 → 不产生重复业务写入。"""
        db = make_db()
        registry = make_registry()
        _, ledger, orchestrator = make_execution_stack(db, registry)
        plan = default_plan(run_id="run-rerun")
        await ledger.init_run(plan)
        first = await orchestrator.run(plan, user_id="u1")
        assert first.status == "completed"

        exec_counts = {n: len(adapter_by_name(registry, n).executions) for n in registry.names()}
        second = await orchestrator.run(plan, user_id="u1")
        # 终态不可领取 → lease_conflict 失败，但 Worker 不被执行
        assert second.status == "failed"
        for name in registry.names():
            assert len(adapter_by_name(registry, name).executions) == exec_counts[name]
