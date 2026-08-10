"""流水线恢复集成测试 — 阶段三 Step 10。

覆盖：
  - 恢复流程：跳过已验证 succeeded、接管过期 running、仅执行 pending/retryable；
  - 硬门禁 2：恢复后重复业务写入 0（succeeded 步骤不重跑）；
  - 硬门禁 3：单 Worker 失败不重跑无依赖已完成步骤；
  - reconciliation：不一致入修复队列，不盲目重跑。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.execution_step_ledger import LeaseConflictError

from tests.integration._multi_agent_helpers import (
    adapter_by_name,
    default_plan,
    fixed_now,
    make_db,
    make_execution_stack,
    make_registry,
)


class TestRecovery:
    async def test_completed_run_recovery_has_nothing_to_execute(self):
        """硬门禁 2：成功 run 恢复后 to_execute 为空，业务写入 0 重复。"""
        db = make_db()
        registry = make_registry()
        _, ledger, orchestrator = make_execution_stack(db, registry)

        plan = default_plan()
        await ledger.init_run(plan)
        outcome = await orchestrator.run(plan, user_id="u1")
        assert outcome.status == "completed"

        executions_before = {
            name: len(adapter_by_name(registry, name).executions)
            for name in registry.names()
        }

        async def _load_plan(run_id: str):
            return plan

        async def _verify(entry):
            return True

        result = await ledger.recover_run(
            run_id="run-1",
            owner_id="recoverer",
            load_plan=_load_plan,
            verify_artifact=_verify,
        )
        assert result.ok
        assert result.to_execute == []
        assert result.skipped and len(result.skipped) == 8
        assert result.taken_over == []
        # 已完成的步骤没有再次执行 → 无重复业务写入
        for name in registry.names():
            assert len(adapter_by_name(registry, name).executions) == executions_before[name]

    async def test_single_worker_failure_does_not_rerun_completed_steps(self):
        """硬门禁 3：score 死信后恢复，仅重放 score 与其下游，已完成步骤不重跑。"""
        db = make_db()
        registry = make_registry(overrides={"score": {"status": "failed", "retryable": True}})
        _, ledger, orchestrator = make_execution_stack(db, registry)

        plan = default_plan()
        await ledger.init_run(plan)
        outcome = await orchestrator.run(plan, user_id="u1")
        # score 重试耗尽 → 死信 → run partial
        assert outcome.status == "partial"
        by_step = {s.step_id: s for s in outcome.steps}
        assert by_step["s5_score"].status == "dead_lettered"
        # 下游 required 依赖失败 → 跳过；review 因可选 rewrite 被跳过仍会执行
        assert by_step["s6_draft"].status == "skipped"

        crawl_execs = len(adapter_by_name(registry, "crawl").executions)
        classify_execs = len(adapter_by_name(registry, "classify").executions)

        async def _load_plan(run_id: str):
            return plan

        result = await ledger.recover_run(
            run_id="run-1", owner_id="recoverer", load_plan=_load_plan
        )
        # 只重放死信 + 其下游 pending（draft/quality_check/review 已被置 skipped，不重放）
        replayable = {e.step_id for e in result.to_execute}
        assert replayable == {"s5_score"}
        # 已完成的无依赖步骤没有被重跑
        assert len(adapter_by_name(registry, "crawl").executions) == crawl_execs
        assert len(adapter_by_name(registry, "classify").executions) == classify_execs

    async def test_takes_over_expired_running_increments_fencing(self):
        db = make_db()
        registry = make_registry()
        _, ledger, orchestrator = make_execution_stack(db, registry)
        plan = default_plan()
        await ledger.init_run(plan)

        # 旧执行者领取后租约过期
        claim = await ledger.begin_attempt(
            run_id="run-1", step_id="s1_crawl", owner_id="old-owner", attempt=1,
            now=fixed_now() - timedelta(seconds=300),
        )
        assert claim.fencing_token == 1

        async def _load_plan(run_id: str):
            return plan

        result = await ledger.recover_run(
            run_id="run-1", owner_id="recoverer", load_plan=_load_plan,
            now=fixed_now(),
        )
        assert result.taken_over and result.taken_over[0].step_id == "s1_crawl"
        taken = result.taken_over[0]
        # fencing 递增：旧 Worker 的迟到写必须被拒绝
        assert taken.fencing_token == 2
        assert taken.lease_owner == "recoverer"

        from agent.worker_registry import WorkerResult

        stale = WorkerResult(step_id="s1_crawl", worker="crawl", status="succeeded", attempt=1)
        with pytest.raises(LeaseConflictError):
            await ledger.complete(
                run_id="run-1", step_id="s1_crawl", owner_id="old-owner",
                fencing_token=1, result=stale,
            )
        # 新 owner 以新 token 提交成功
        fresh = WorkerResult(
            step_id="s1_crawl", worker="crawl", status="succeeded",
            attempt=2, idempotency_key="k", input_hash="h", result_hash="r",
        )
        entry = await ledger.complete(
            run_id="run-1", step_id="s1_crawl", owner_id="recoverer",
            fencing_token=2, result=fresh,
        )
        assert entry.status == "succeeded"

    async def test_recovery_fails_without_plan(self):
        db = make_db()
        registry = make_registry()
        _, ledger, _ = make_execution_stack(db, registry)
        plan = default_plan()
        await ledger.init_run(plan)

        async def _missing(run_id: str):
            return None

        result = await ledger.recover_run(run_id="run-1", owner_id="r", load_plan=_missing)
        assert result.ok is False
        assert any("plan" in issue for issue in result.issues)

    async def test_recovery_only_executes_pending_and_retryable(self):
        db = make_db()
        registry = make_registry()
        _, ledger, orchestrator = make_execution_stack(db, registry)
        plan = default_plan()
        await ledger.init_run(plan)

        # 手工制造混合状态：s1 成功、s2 失败(可重试)、s3 pending、s4 skipped
        claim1 = await ledger.begin_attempt(run_id="run-1", step_id="s1_crawl", owner_id="o", attempt=1)
        from agent.worker_registry import WorkerResult

        await ledger.complete(
            run_id="run-1", step_id="s1_crawl", owner_id="o",
            fencing_token=claim1.fencing_token,
            result=WorkerResult(step_id="s1_crawl", worker="crawl", status="succeeded", attempt=1),
        )
        claim2 = await ledger.begin_attempt(run_id="run-1", step_id="s3_classify", owner_id="o", attempt=1)
        await ledger.fail(
            run_id="run-1", step_id="s3_classify", owner_id="o",
            fencing_token=claim2.fencing_token, status="failed",
            error_type="boom", retryable=True,
        )
        await ledger.skip(run_id="run-1", step_id="s4_filter", reason="skipped manually")

        async def _load_plan(run_id: str):
            return plan

        async def _verify(entry):
            return True

        result = await ledger.recover_run(
            run_id="run-1", owner_id="r", load_plan=_load_plan, verify_artifact=_verify,
        )
        runnable = {e.step_id for e in result.to_execute}
        skipped_ids = {e.step_id for e in result.skipped}
        assert "s1_crawl" in skipped_ids      # 已验证 succeeded → 跳过
        assert "s3_classify" in runnable      # failed 可重试 → 执行
        assert "s5_score" in runnable         # pending → 执行
        assert "s4_filter" not in runnable    # skipped 不执行


class TestReconciliation:
    async def test_reconcile_queues_repair_not_rerun(self):
        """checkpoint/ledger 不一致入修复队列，不盲目重跑。"""
        db = make_db()
        registry = make_registry()
        _, ledger, orchestrator = make_execution_stack(db, registry)
        plan = default_plan()
        await ledger.init_run(plan)

        # 全部成功
        outcome = await orchestrator.run(plan, user_id="u1")
        assert outcome.status == "completed"

        # 删除一条 ledger 记录，模拟 checkpoint 有而 ledger 无
        ledger_col = db["execution_step_ledger"]
        removed = [d for d in ledger_col.docs if d["step_id"] == "s6_draft"]
        ledger_col.docs.remove(removed[0])

        async def _load_plan(run_id: str):
            return plan

        async def _read_checkpoint(run_id: str):
            return {
                "completed_steps": [
                    "s1_crawl", "s3_classify", "s4_filter", "s5_score",
                    "s6_draft", "s7_quality_check", "s9_review",
                ]
            }

        result = await ledger.reconcile(
            run_id="run-1",
            load_plan=_load_plan,
            read_checkpoint=_read_checkpoint,
        )
        issue_types = {i.issue_type for i in result.issues}
        assert "missing_ledger" in issue_types
        assert result.repair_enqueued >= 1
        # 修复队列落库，状态 open
        repair = db["ledger_repair_queue"].docs
        assert repair and all(d["status"] == "open" for d in repair)
        # 没有重跑任何步骤（executions 保持）
        for name in registry.names():
            adapter = adapter_by_name(registry, name)
            assert len(adapter.executions) == 1
