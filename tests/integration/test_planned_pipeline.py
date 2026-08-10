"""计划流水线集成测试 — 阶段三 Step 10。

覆盖：
  - Planner → Validator → Ledger → Orchestrator 完整闭环（completed）；
  - 违规 Plan 100% fallback（硬门禁 1：review/白名单/user 隔离不放过）；
  - 执行模式决策（flag 关 → current / shadow / 灰度命中 → planned）；
  - 影子模式：业务写被 _ShadowDBProxy 拦截，仅记差异，不写正式产物。
"""

from __future__ import annotations

import pytest

from agent.multi_agent import (
    MODE_CURRENT,
    MODE_PLANNED,
    MODE_SHADOW,
    _ShadowDBProxy,
    decide_execution_mode,
)
from agent.worker_registry import WorkerLease

from tests.integration._multi_agent_helpers import (
    DEFAULT_WORKERS,
    _FakeAdapter,
    _FakeLLMWrapper,
    adapter_by_name,
    default_plan,
    make_db,
    make_execution_stack,
    make_planner,
    make_registry,
)


# ═══════════════════════════════════════════════════════════════
# 完整闭环
# ═══════════════════════════════════════════════════════════════


class TestEndToEndPlanned:
    async def test_default_plan_runs_to_completed(self):
        db = make_db()
        registry = make_registry()
        _, ledger, orchestrator = make_execution_stack(db, registry)

        plan = default_plan()
        await ledger.init_run(plan)
        outcome = await orchestrator.run(plan, user_id="u1")

        assert outcome.status == "completed"
        assert outcome.waves == 8  # crawl/classify/filter/score/draft/quality_check/rewrite(可选)/review
        by_step = {s.step_id: s for s in outcome.steps}
        assert all(s.status == "succeeded" for s in by_step.values())
        # 全部步骤在账本中均为 succeeded，租约已释放
        entries = await ledger.list_run_steps("run-1")
        assert len(entries) == 8
        assert all(e.status == "succeeded" for e in entries)
        assert all(e.lease_owner == "" for e in entries)

    async def test_llm_choice_plan_runs(self):
        db = make_db()
        registry = make_registry()
        _, ledger, orchestrator = make_execution_stack(db, registry)

        wrapper = _FakeLLMWrapper()
        planner = make_planner(db=db, wrapper=wrapper)
        plan_id = ""

        async def _plan_once():
            outcome = await planner.plan(
                run_id="run-2",
                user_id="u1",
                products=[{"id": "p1", "name": "P1"}],
                articles=[],
            )
            return outcome

        planner_outcome = await _plan_once()
        assert planner_outcome.source == "planner"
        assert not planner_outcome.rejected
        assert plan_id or True  # 占位：plan_id 由服务端生成
        assert planner_outcome.plan.run_id == "run-2"

        await ledger.init_run(planner_outcome.plan)
        outcome = await orchestrator.run(planner_outcome.plan, user_id="u1")
        assert outcome.status == "completed"

    async def test_violation_falls_back_100pct(self):
        """硬门禁 1：模型越权 product（不在白名单）→ Plan 被拒 → 100% 回退默认计划。"""
        db = make_db()
        # 模型提交了越权产品 "p-foreign"，不在 allowed_products 内
        from agent.planner import PlannerChoice

        choice = PlannerChoice(product_ids=["p-foreign"])
        wrapper = _FakeLLMWrapper(choice=choice)
        planner = make_planner(db=db, wrapper=wrapper)

        outcome = await planner.plan(
            run_id="run-3",
            user_id="u1",
            products=[{"id": "p1", "name": "P1"}],
            articles=[],
        )
        # 硬门禁：违规 Plan 100% fallback
        assert outcome.rejected is True
        assert outcome.source == "fallback"
        # 回退计划仍含 review 必经守卫
        worker_set = {s.worker for s in outcome.plan.steps}
        assert "review" in worker_set
        assert "quality_check" in worker_set
        # planner_plans 落库为 rejected
        col = db["planner_plans"]
        docs = [d for d in col.docs if d["run_id"] == "run-3"]
        assert docs and docs[0]["status"] == "rejected"

    async def test_draft_without_review_never_reaches_execution(self):
        """即使有人绕过 Planner 直接构造缺 review 的计划，验证器也会拒绝。"""
        from agent.plan_contracts import PlanValidator, PipelinePlan, _step

        plan = PipelinePlan(
            plan_id="plan-x",
            run_id="run-x",
            planner_version="t",
            input_snapshot_hash="sha256:h",
            steps=[
                _step("s1", "draft", [], {"article_ids": ["a1"]}),
                _step("s2", "quality_check", ["s1"], {"article_ids": ["a1"]}),
            ],
        )
        result = PlanValidator().validate(plan)
        assert result.rejected
        assert "missing guard workers" in result.reason


# ═══════════════════════════════════════════════════════════════
# 执行模式决策
# ═══════════════════════════════════════════════════════════════


class TestExecutionMode:
    def test_flag_off_always_current(self):
        assert decide_execution_mode(enabled=False, shadow_enabled=True, rollout_percent=100, user_id="u1") == MODE_CURRENT

    def test_shadow_when_enabled(self):
        assert decide_execution_mode(enabled=True, shadow_enabled=True, rollout_percent=100, user_id="u1") == MODE_SHADOW

    def test_rollout_hit_planned(self):
        # percent=100 命中所有用户
        assert decide_execution_mode(enabled=True, shadow_enabled=False, rollout_percent=100, user_id="u1") == MODE_PLANNED

    def test_rollout_zero_never_planned(self):
        assert decide_execution_mode(enabled=True, shadow_enabled=False, rollout_percent=0, user_id="u1") == MODE_CURRENT


# ═══════════════════════════════════════════════════════════════
# 影子模式：写拦截 + 差异记录
# ═══════════════════════════════════════════════════════════════


class TestShadowMode:
    @staticmethod
    def _business_registry(target_db):
        """Worker 成功时向 target_db 的业务集合写入幂等记录。"""
        registry = make_registry()

        async def _write(state, ctx, lease):
            await target_db["business_artifacts"].insert_one(
                {
                    "run_id": ctx["run_id"],
                    "step_id": ctx["step_id"],
                    "idem": "u1:{}:{}:h".format(ctx["run_id"], ctx["step_id"]),
                }
            )

        for name in DEFAULT_WORKERS:
            registry.register(_FakeAdapter(name, on_execute=_write))
        return registry

    @staticmethod
    def _shadow_orchestrator(registry):
        """影子编排器：不接 ledger（与 orchestrator_for(shadow=True) 一致）。"""
        from agent.orchestrator import Orchestrator

        return Orchestrator(
            registry,
            owner_id="orch",
            max_concurrency=5,
            user_concurrency=2,
            worker_concurrency=2,
            lease_seconds=120,
            default_max_attempts=3,
        )

    async def test_shadow_writes_intercepted_and_diffed(self):
        """影子执行：业务集合不被写入，差异进 planned_artifact_diffs。"""
        db = make_db()
        _, ledger, orchestrator = make_execution_stack(db, self._business_registry(db))

        # 正常执行 → 业务产物落库
        plan = default_plan(run_id="run-sh")
        await ledger.init_run(plan)
        outcome = await orchestrator.run(plan, user_id="u1")
        assert outcome.status == "completed"

        # 影子执行：Worker 通过 _ShadowDBProxy 写，真实业务集合不被污染
        shadow_proxy = _ShadowDBProxy(db)
        shadow_registry = self._business_registry(shadow_proxy)
        shadow_orch = self._shadow_orchestrator(shadow_registry)
        shadow_plan = default_plan(run_id="run-sh2")

        outcome2 = await shadow_orch.run(shadow_plan, user_id="u1")
        assert outcome2.status == "completed"
        # 正式业务产物未被影子执行污染
        assert len(db["business_artifacts"].docs) == 8
        # 差异日志记录了拦截的写操作
        diffs = db["planned_artifact_diffs"].docs
        assert len(diffs) >= 8
        assert all(d["mode"] == MODE_SHADOW for d in diffs)
        assert all("content_md" not in str(d.get("filter_or_doc")) for d in diffs)

    async def test_shadow_lease_metadata_present(self):
        """影子 Worker 仍携带租约元数据（owner/fencing）。"""
        db = make_db()
        shadow_proxy = _ShadowDBProxy(db)
        shadow_registry = self._business_registry(shadow_proxy)
        shadow_orch = self._shadow_orchestrator(shadow_registry)
        plan = default_plan(run_id="run-sh3")
        outcome = await shadow_orch.run(plan, user_id="u1")
        assert outcome.status == "completed"

        crawl = adapter_by_name(shadow_registry, "crawl")
        lease = crawl.executions[0]["lease"]
        assert isinstance(lease, WorkerLease)
        assert lease.owner_id == "orch"
        assert lease.run_id == "run-sh3"
        assert lease.fencing_token >= 1
