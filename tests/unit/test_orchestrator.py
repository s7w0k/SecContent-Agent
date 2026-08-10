"""Orchestrator 单元测试 — 阶段三 Step 5。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from agent.orchestrator import Orchestrator, OrchestratorError, build_waves
from agent.plan_contracts import PipelinePlan, PlanStep
from agent.worker_registry import WorkerAdapter, WorkerRegistry, WorkerResult, WorkerSpec

# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _step(
    step_id: str,
    worker: str,
    depends_on: list[str] | None = None,
    policy: str = "required",
    timeout_s: int = 30,
    max_attempts: int = 3,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        worker=worker,  # type: ignore[arg-type]
        depends_on=depends_on or [],
        input_refs={},
        policy=policy,  # type: ignore[arg-type]
        timeout_s=timeout_s,
        max_attempts=max_attempts,
    )


def _plan(steps: list[PlanStep]) -> PipelinePlan:
    return PipelinePlan(
        plan_id="plan-1",
        run_id="run-1",
        planner_version="test-v1",
        input_snapshot_hash="h" * 64,
        steps=steps,
    )


def _ok_result(ctx: dict) -> WorkerResult:
    return WorkerResult(
        step_id=ctx["step_id"],
        worker=ctx["worker"],
        idempotency_key="k",
        input_hash="i",
        result_hash="r",
        status="succeeded",
        attempt=ctx["attempt"],
    )


def _fail_result(ctx: dict, retryable: bool = False, error_type: str = "boom") -> WorkerResult:
    return WorkerResult(
        step_id=ctx["step_id"],
        worker=ctx["worker"],
        idempotency_key="k",
        input_hash="i",
        result_hash="",
        status="failed",
        error_type=error_type,
        retryable=retryable,
        attempt=ctx["attempt"],
    )


class _FakeAdapter(WorkerAdapter):
    def __init__(self, name: str, behavior=None, **spec_kwargs):
        self.name = name  # type: ignore[assignment]
        self.version = "v1"
        self.spec = WorkerSpec(name=name, **spec_kwargs)  # type: ignore[arg-type]
        self.behavior = behavior or (lambda state, ctx, lease: _ok_result(ctx))

    async def execute(self, state, ctx, lease=None) -> WorkerResult:
        result = self.behavior(state, ctx, lease)
        if asyncio.iscoroutine(result):
            return await result
        return result


def _make_orchestrator(registry: WorkerRegistry, **kwargs) -> Orchestrator:
    return Orchestrator(registry, **kwargs)


async def _run(plan, registry, **kwargs):
    orchestrator = _make_orchestrator(registry, **kwargs.pop("orchestrator_kwargs", {}))
    return await orchestrator.run(plan, **kwargs)


# ═══════════════════════════════════════════════════════════════
# build_waves
# ═══════════════════════════════════════════════════════════════


class TestBuildWaves:
    def test_linear_plan(self):
        plan = _plan(
            [_step("s1", "crawl"), _step("s2", "classify", ["s1"]), _step("s3", "filter", ["s2"])]
        )
        waves = build_waves(plan)
        assert [[s.step_id for s in w] for w in waves] == [["s1"], ["s2"], ["s3"]]

    def test_parallel_wave(self):
        plan = _plan([_step("a", "crawl"), _step("b", "filter")])
        waves = build_waves(plan)
        assert {s.step_id for s in waves[0]} == {"a", "b"}

    def test_multi_dep_wave(self):
        plan = _plan(
            [_step("a", "crawl"), _step("b", "filter"), _step("c", "classify", ["a", "b"])]
        )
        waves = build_waves(plan)
        assert [sorted(s.step_id for s in w) for w in waves] == [
            ["a", "b"],
            ["c"],
        ]

    def test_cycle_raises(self):
        plan = _plan([_step("s1", "crawl", ["s2"]), _step("s2", "filter", ["s1"])])
        with pytest.raises(OrchestratorError):
            build_waves(plan)


# ═══════════════════════════════════════════════════════════════
# 执行与并行
# ═══════════════════════════════════════════════════════════════


class TestExecution:
    def test_completed_run(self):
        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=lambda s, c, lease: _ok_result(c)))
        plan = _plan([_step("s1", "crawl")])
        outcome = asyncio.run(_run(plan, registry, user_id="u-1"))
        assert outcome.status == "completed"
        assert outcome.steps[0].status == "succeeded"
        assert outcome.waves == 1

    def test_parallel_steps_share_wave(self):
        active = {"v": 0, "max": 0}

        async def behavior(state, ctx, lease):
            active["v"] += 1
            active["max"] = max(active["max"], active["v"])
            await asyncio.sleep(0.05)
            active["v"] -= 1
            return _ok_result(ctx)

        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=behavior, concurrency_group="local"))
        registry.register(_FakeAdapter("filter", behavior=behavior, concurrency_group="local"))
        plan = _plan([_step("a", "crawl"), _step("b", "filter")])
        outcome = asyncio.run(_run(plan, registry, user_id="u-1"))
        assert outcome.status == "completed"
        assert active["max"] == 2  # 同一波并行

    def test_global_concurrency_limit(self):
        active = {"v": 0, "max": 0}

        async def behavior(state, ctx, lease):
            active["v"] += 1
            active["max"] = max(active["max"], active["v"])
            await asyncio.sleep(0.05)
            active["v"] -= 1
            return _ok_result(ctx)

        registry = WorkerRegistry()
        for name in ("crawl", "filter", "classify", "score", "draft"):
            registry.register(_FakeAdapter(name, behavior=behavior, concurrency_group="local"))
        plan = _plan([_step(s, s) for s in ("crawl", "filter", "classify", "score", "draft")])
        outcome = asyncio.run(
            _run(plan, registry, user_id="u-1", orchestrator_kwargs={"max_concurrency": 2})
        )
        assert outcome.status == "completed"
        assert active["max"] == 2  # 全局配额 2


# ═══════════════════════════════════════════════════════════════
# 失败 / 重试 / 策略
# ═══════════════════════════════════════════════════════════════


class TestFailureAndRetry:
    def test_required_failure_blocks_dependents(self):
        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=lambda s, c, lease: _fail_result(c)))
        registry.register(_FakeAdapter("filter", behavior=lambda s, c, lease: _ok_result(c)))
        plan = _plan(
            [
                _step("s1", "crawl", max_attempts=1),
                _step("s2", "filter", ["s1"]),
            ]
        )
        outcome = asyncio.run(_run(plan, registry, user_id="u-1"))
        assert outcome.status == "failed"
        assert outcome.steps[0].status == "failed"
        assert outcome.steps[1].status == "skipped"
        assert outcome.steps[1].reason == "dependency failed"

    def test_optional_failure_continues(self):
        registry = WorkerRegistry()
        registry.register(_FakeAdapter("enrich", behavior=lambda s, c, lease: _fail_result(c)))
        registry.register(_FakeAdapter("filter", behavior=lambda s, c, lease: _ok_result(c)))
        plan = _plan(
            [
                _step("s1", "enrich", policy="optional", max_attempts=1),
                _step("s2", "filter", ["s1"]),
            ]
        )
        outcome = asyncio.run(_run(plan, registry, user_id="u-1"))
        assert outcome.status == "completed"
        assert outcome.steps[0].status == "skipped"  # optional 失败→跳过
        assert outcome.steps[1].status == "succeeded"

    def test_retry_then_success(self):
        calls = {"n": 0}

        async def behavior(state, ctx, lease):
            calls["n"] += 1
            if calls["n"] == 1:
                return _fail_result(ctx, retryable=True)
            return _ok_result(ctx)

        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=behavior, max_attempts=3, retry_safe=True))
        plan = _plan([_step("s1", "crawl")])
        outcome = asyncio.run(_run(plan, registry, user_id="u-1"))
        assert outcome.status == "completed"
        assert outcome.steps[0].status == "succeeded"
        assert outcome.steps[0].attempt == 2

    def test_dead_letter_after_retries_exhausted(self):
        async def behavior(state, ctx, lease):
            return _fail_result(ctx, retryable=True)

        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=behavior, max_attempts=2, retry_safe=True))
        plan = _plan([_step("s1", "crawl")])
        outcome = asyncio.run(_run(plan, registry, user_id="u-1"))
        assert outcome.status == "partial"
        assert outcome.steps[0].status == "dead_lettered"
        assert outcome.steps[0].attempt == 2

    def test_non_retryable_failure(self):
        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=lambda s, c, lease: _fail_result(c)))
        plan = _plan([_step("s1", "crawl", max_attempts=3)])
        outcome = asyncio.run(_run(plan, registry, user_id="u-1"))
        assert outcome.steps[0].status == "failed"
        assert outcome.steps[0].attempt == 1

    def test_worker_timeout(self):
        async def hang(state, ctx, lease):
            await asyncio.sleep(10)

        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=hang, timeout_s=1, max_attempts=1))
        plan = _plan([_step("s1", "crawl", timeout_s=1, max_attempts=1)])
        outcome = asyncio.run(_run(plan, registry, user_id="u-1"))
        assert outcome.steps[0].status == "dead_lettered"
        assert outcome.steps[0].error_type == "timeout"


# ═══════════════════════════════════════════════════════════════
# cancel / deadline / 租约 / 注册表防御
# ═══════════════════════════════════════════════════════════════


class TestControl:
    def test_cancel_stops_later_waves(self):
        executed: list[str] = []
        started = asyncio.Event()

        async def behavior(state, ctx, lease):
            if ctx["step_id"] == "s1":
                started.set()
                await asyncio.sleep(0.05)  # 给测试线程设置取消事件的时间
            executed.append(ctx["step_id"])
            return _ok_result(ctx)

        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=behavior))
        registry.register(_FakeAdapter("filter", behavior=behavior))
        registry.register(_FakeAdapter("classify", behavior=behavior))
        plan = _plan(
            [_step("s1", "crawl"), _step("s2", "filter", ["s1"]), _step("s3", "classify", ["s2"])]
        )

        async def scenario():
            cancel_event = asyncio.Event()
            orchestrator = _make_orchestrator(registry)
            task = asyncio.ensure_future(
                orchestrator.run(plan, user_id="u-1", cancel_event=cancel_event)
            )
            await started.wait()
            cancel_event.set()
            return await task

        outcome = asyncio.run(scenario())
        assert outcome.status == "canceled"
        assert executed == ["s1"]  # 后续波次未执行

    def test_deadline_cancels(self):
        async def slow(state, ctx, lease):
            await asyncio.sleep(0.2)
            return _ok_result(ctx)

        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=slow))
        plan = _plan([_step("s1", "crawl"), _step("s2", "crawl", ["s1"])])

        import time as time_mod

        deadline = time_mod.monotonic() + 0.05
        outcome = asyncio.run(_run(plan, registry, user_id="u-1", deadline_at=deadline))
        assert outcome.status == "canceled"

    def test_lease_contains_fencing_metadata(self):
        leases = []

        async def behavior(state, ctx, lease):
            leases.append(lease)
            return _ok_result(ctx)

        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=behavior))
        plan = _plan([_step("s1", "crawl")])
        outcome = asyncio.run(
            _run(plan, registry, user_id="u-1", orchestrator_kwargs={"owner_id": "orch-1"})
        )
        assert outcome.status == "completed"
        lease = leases[0]
        assert lease.owner_id == "orch-1"
        assert lease.run_id == "run-1"
        assert lease.step_id == "s1"
        assert lease.fencing_token == 1
        assert lease.expires_at > datetime.now(UTC)

    def test_lease_fencing_token_increments_with_attempts(self):
        leases = []
        calls = {"n": 0}

        async def behavior(state, ctx, lease):
            leases.append((ctx["attempt"], lease.fencing_token))
            calls["n"] += 1
            if calls["n"] == 1:
                return _fail_result(ctx, retryable=True)
            return _ok_result(ctx)

        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=behavior, max_attempts=3, retry_safe=True))
        plan = _plan([_step("s1", "crawl")])
        outcome = asyncio.run(_run(plan, registry, user_id="u-1"))
        assert outcome.status == "completed"
        assert leases == [(1, 1), (2, 2)]  # fencing token 随 attempt 递增

    def test_unregistered_worker_fails(self):
        registry = WorkerRegistry()
        registry.register(_FakeAdapter("crawl", behavior=lambda s, c, lease: _ok_result(c)))
        plan = _plan([_step("s1", "score")])  # score 未注册
        outcome = asyncio.run(_run(plan, registry, user_id="u-1"))
        assert outcome.status == "failed"
        assert outcome.steps[0].status == "failed"
        assert outcome.steps[0].error_type == "unregistered_worker"
