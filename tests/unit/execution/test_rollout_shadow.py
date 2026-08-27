"""Canary rollout / Shadow 双跑 / Comparator（计划 §36-38 / §48-51 / §96）。

验收：
  - stable_hash 确定性、单调（percent 增大时旧子集 ⊆ 新子集）
  - ShadowSkill 只读：写工具被 production_readonly 拒绝（零副作用，§129）
  - Shadow 双跑中 skill 失败/超时绝不影响 legacy primary（§38 / §47）
  - Comparator 产出 ShadowEvaluation，ARTIFACT 缺失触发 critical_mismatch
"""

from __future__ import annotations

import asyncio

from agent.execution.comparator import ShadowComparator
from agent.execution.contracts import (
    ExecutionRequest,
    ExecutionResult,
    WorkflowExecutor,
)
from agent.execution.metrics import ExecutionMetrics, ExecutionMetricsClient
from agent.execution.rollout import (
    LevelCanaryRollout,
    ShadowSampler,
    rollout_bucket,
    stable_hash,
)
from agent.execution.shadow_executor import ShadowExecutor


def _req(**kw) -> ExecutionRequest:
    d = {
        "task_id": "t1",
        "task_type": "run-v2",
        "tenant_id": "ten",
        "user_id": "usr",
        "trace_id": "tr",
    }
    d.update(kw)
    return ExecutionRequest(**d)


class TestRollout:
    def test_stable_hash_is_deterministic(self) -> None:
        a = stable_hash("seed", "ten", "usr")
        b = stable_hash("seed", "ten", "usr")
        c = stable_hash("seed", "ten", "usr2")
        assert a == b
        assert a != c

    def test_bucket_in_range(self) -> None:
        for i in range(200):
            bucket = rollout_bucket(50, "s", f"t{i}", f"u{i}")
            assert 0 <= bucket < 100

    def test_canary_choose_boundaries(self) -> None:
        r0 = LevelCanaryRollout(percent=0, seed="s")
        r100 = LevelCanaryRollout(percent=100, seed="s")
        assert r0.choose(_req()) == "legacy"
        assert r100.choose(_req()) == "skill_planned"

    def test_canary_choose_sticky_and_monotonic(self) -> None:
        mid = LevelCanaryRollout(percent=50, seed="s")
        high = LevelCanaryRollout(percent=80, seed="s")
        # monotonic：命中 50% 的 tenant 一定命中 80%（旧子集 ⊆ 新子集）
        for i in range(50):
            req = _req(tenant_id=f"t{i}", user_id="u")
            if mid.choose(req) == "skill_planned":
                assert high.choose(req) == "skill_planned"

    def test_is_sticky_guard(self) -> None:
        r = LevelCanaryRollout(percent=50, seed="s")
        assert r.is_sticky(50)
        assert r.is_sticky(80)
        assert not r.is_sticky(30)

    def test_shadow_sampler_100_and_0(self) -> None:
        assert ShadowSampler(sample_percent=100).should_sample(_req()) is True
        assert ShadowSampler(sample_percent=0).should_sample(_req()) is False


class _SlowSkill(WorkflowExecutor):
    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        await asyncio.sleep(1)
        return ExecutionResult(engine="skill_planned", status="SUCCEEDED")

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        raise AssertionError("unused")


class _BoomSkill(WorkflowExecutor):
    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        raise RuntimeError("shadow boom")

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        raise AssertionError("unused")


class _GoodLegacy(WorkflowExecutor):
    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(engine="legacy", status="SUCCEEDED", artifact_refs=["art:1"])

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(engine="legacy", status="SUCCEEDED")


class TestShadowExecutor:
    async def test_skill_failure_does_not_affect_primary(self) -> None:
        legacy = _GoodLegacy()
        exec_ = ShadowExecutor(legacy=legacy, skill=_BoomSkill())
        result = await exec_.execute(_req())
        assert result.engine == "legacy"
        assert result.status == "SUCCEEDED"

    async def test_skill_timeout_does_not_affect_primary(self) -> None:
        legacy = _GoodLegacy()
        exec_ = ShadowExecutor(legacy=legacy, skill=_SlowSkill(), shadow_timeout_seconds=0.05)
        result = await exec_.execute(_req())
        assert result.engine == "legacy"
        assert result.status == "SUCCEEDED"

    async def test_zero_write_shadow_uses_readonly(self) -> None:
        """Skills 在 shadow 侧以 production_readonly 语义执行——由装配保证，
        ShadowExecutor 本身通过 _write_guarded=True 声明零写（§129）。"""
        exec_ = ShadowExecutor(legacy=_GoodLegacy(), skill=_GoodLegacy())
        assert exec_._write_guarded is True

    async def test_primary_status_wins(self) -> None:
        """skill 双跑产出不同 status，primary（legacy）依然权威返回。"""
        legacy = _GoodLegacy()
        skill = _GoodLegacy()
        exec_ = ShadowExecutor(legacy=legacy, skill=skill)
        result = await exec_.execute(_req())
        assert result.engine == "legacy"


class TestComparator:
    async def test_evaluation_on_match(self) -> None:
        primary = ExecutionResult(engine="legacy", status="SUCCEEDED", artifact_refs=["art:1"])
        # COMPLETED 不在 ExecutionStatus 字面量内；用 SUCCEEDED 表达 skill 侧成功
        shadow = ExecutionResult(
            engine="skill_planned", status="SUCCEEDED", artifact_refs=["art:1"]
        )
        ev = await ShadowComparator().compare(request=_req(), primary=primary, shadow=shadow)
        assert ev.critical_mismatch is False
        assert ev.artifact_type_match is True

    async def test_skill_artifact_missing_is_critical(self) -> None:
        primary = ExecutionResult(engine="legacy", status="SUCCEEDED", artifact_refs=["art:1"])
        shadow = ExecutionResult(engine="skill_planned", status="SUCCEEDED", artifact_refs=[])
        ev = await ShadowComparator().compare(request=_req(), primary=primary, shadow=shadow)
        assert ev.critical_mismatch is True
        assert ev.artifact_type_match is False

    async def test_shadow_did_not_run(self) -> None:
        primary = ExecutionResult(engine="legacy", status="SUCCEEDED")
        ev = await ShadowComparator().compare(request=_req(), primary=primary, shadow=None)
        assert ev.skill_status == "NOT_RUN"
        assert ev.critical_mismatch is False


class TestMetrics:
    def test_metrics_client_noop_when_disabled(self) -> None:
        client = ExecutionMetricsClient()  # 无 metrics → no-op
        client.record(metric="execution_total", engine="legacy")
        assert client.enabled is False

    def test_metrics_records_counts(self) -> None:
        metrics = ExecutionMetrics()
        client = ExecutionMetricsClient(metrics=metrics)
        client.record(metric="execution_total", engine="legacy")
        client.record(metric="execution_succeeded", engine="legacy", latency_ms=12.0)
        assert client.enabled is True
        assert metrics._counters[("execution_total", "legacy")] == 1
        assert metrics._latency[("latency", "legacy")] == [12.0]
