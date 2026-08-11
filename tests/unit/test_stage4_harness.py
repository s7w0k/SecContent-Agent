"""阶段4 Harness、灰度与生产上线 — 单元测试（WBS 4.1-4.6）。

覆盖：
  - observability：MetricCollector / SLI-SLO / AlertRule + AlertEvaluator
  - tool_harness：ToolContract / ToolRegistry / 净化 / 录制重放 / ProtectedToolCaller
  - model_harness：错误映射 / ModelRateLimiter / ModelHarness（allowlist+fallback+熔断）
  - context_harness：可重复构建 / legacy-candidate diff / token 偏差 / 审计
  - eval_harness：EvalSnapshot 完整性 / MatrixRunner 聚合 / MinimalRepro 复现包
  - fault_harness：11 类故障注入 / 演练编排
  - rollout_controller：灰度追踪双条件 / 自动回滚决策与应用
  - capacity：容量模型输出 / 灰度档位成本 / 负载模拟
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent.circuit_breaker import CircuitBreakerRegistry, CircuitConfig
from agent.context_manager import ContextRequest, ContextSource
from agent.evals.contracts import EvalCase, EvalResult, PairedEvalResult
from agent.harness.capacity import (
    SCENARIO_PRESETS,
    CapacityInputs,
    CapacityModel,
    LoadScenario,
    run_load_simulation,
)
from agent.harness.context_harness import ContextHarness
from agent.harness.eval_harness import (
    EvalSnapshot,
    MatrixCell,
    MatrixReport,
    MatrixRunner,
    MinimalRepro,
)
from agent.harness.fault_harness import (
    FAULT_SCENARIOS,
    FaultDrillRunner,
    FaultInjected,
    FaultInjector,
    FaultSpec,
    FaultType,
    ProcessKilled,
)
from agent.harness.model_harness import (
    FakeModelAdapter,
    FinishReason,
    ModelErrorKind,
    ModelHarness,
    ModelRateLimiter,
    UsageSnapshot,
    map_model_error,
)
from agent.harness.observability import (
    ALERT_ACTION_BREAKER_CAPABILITY,
    ALERT_ACTION_ROLLBACK,
    ALERT_ACTION_STOP_NEW,
    AlertEvaluator,
    AlertRule,
    HistSummary,
    MetricCollector,
    Sli,
    Slo,
    default_alert_rules,
)
from agent.harness.rollout_controller import (
    ACTION_NONE,
    ACTION_ROLLBACK,
    FeatureGate,
    RollbackController,
    RolloutStage,
    RolloutTracker,
)
from agent.harness.tool_harness import (
    FakeToolAdapter,
    ProtectedToolCaller,
    RecordedToolAdapter,
    RecordedToolLog,
    SandboxToolAdapter,
    SideEffectLevel,
    ToolContract,
    ToolRegistry,
    ToolRegistryError,
    ToolResultSanitizer,
)

FIXED_NOW = datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)


# ═══════════════════════════════════════════════════════════════
# observability
# ═══════════════════════════════════════════════════════════════


class TestMetricCollector:
    def test_counter_inc_and_read(self):
        m = MetricCollector()
        m.inc("tool_calls", tool="search", status="ok")
        m.inc("tool_calls", tool="search", status="ok")
        m.inc("tool_calls", tool="search", status="error")
        assert m.counter("tool_calls", tool="search", status="ok") == 2
        assert m.counter("tool_calls", tool="search", status="error") == 1
        assert m.counter("tool_calls", tool="other", status="ok") == 0

    def test_gauge_set_and_get(self):
        m = MetricCollector()
        m.set_gauge("queue_depth", value=3, worker="w1")
        assert m.gauge("queue_depth", worker="w1") == 3.0
        m.set_gauge("queue_depth", value=1, worker="w1")
        assert m.gauge("queue_depth", worker="w1") == 1.0

    def test_histogram_summary(self):
        m = MetricCollector()
        for v in (1.0, 2.0, 3.0, 4.0, 100.0):
            m.observe("tool_latency_ms", value=v, tool="search")
        h = m.histogram("tool_latency_ms", tool="search")
        assert isinstance(h, HistSummary)
        assert h.count == 5
        assert h.min == 1.0
        assert h.max == 100.0
        assert h.p95 == 100.0  # 5 个样本的 95 分位 = 最大
        assert h.p50 == 3.0
        assert m.histogram("missing").count == 0

    def test_reset(self):
        m = MetricCollector()
        m.inc("x")
        m.reset()
        assert m.counter("x") == 0

    def test_snapshot_shape(self):
        m = MetricCollector()
        m.inc("a", tool="t")
        m.observe("b", value=1.0)
        snap = m.snapshot()
        assert "a|tool=t" in snap["counters"]
        assert "b" in snap["histograms"]


class TestSliSlo:
    def test_ratio_defaults_to_one_when_empty(self):
        assert Sli(name="x").ratio() == 1.0

    def test_ratio(self):
        assert Sli(name="x", good_events=90, total_events=100).ratio() == 0.9

    def test_slo_met(self):
        slo = Slo(name="availability", sli="good", target_ratio=0.99)
        assert slo.met(0.995)
        assert not slo.met(0.9)


class TestAlertEvaluator:
    def test_rule_matches_ops(self):
        assert AlertRule("r", "m", ">", 5.0).matches(6.0)
        assert AlertRule("r", "m", ">", 5.0).matches(5.5)
        assert not AlertRule("r", "m", ">", 5.0).matches(5.0)
        assert AlertRule("r", "m", ">=", 5.0).matches(5.0)
        assert AlertRule("r", "m", "<", 5.0).matches(4.0)
        assert AlertRule("r", "m", "<=", 5.0).matches(5.0)
        assert AlertRule("r", "m", "==", 5.0).matches(5.0)

    def test_default_rules_eight(self):
        rules = default_alert_rules()
        assert len(rules) == 8

    def test_evaluate_filters_missing_metrics(self):
        ev = AlertEvaluator(rules=[AlertRule("r1", "latency_p95_seconds", ">", 5.0)])
        assert ev.evaluate({"latency_p95_seconds": 10.0})  # 触发
        assert ev.evaluate({}) == []  # 指标缺失不触发

    def test_evaluate_disabled_rule(self):
        rule = AlertRule("r1", "x", ">", 0.0, enabled=False)
        ev = AlertEvaluator(rules=[rule])
        assert ev.evaluate({"x": 1.0}) == []

    def test_worst_action_none_when_clean(self):
        ev = AlertEvaluator(rules=default_alert_rules())
        assert ev.worst_action({}) is None

    def test_worst_action_rollback_on_latency(self):
        ev = AlertEvaluator(rules=default_alert_rules(latency_p95_slo_seconds=5.0))
        assert ev.worst_action({"latency_p95_seconds": 10.0}) == ALERT_ACTION_ROLLBACK

    def test_worst_action_stop_new_on_unsafe(self):
        ev = AlertEvaluator(rules=default_alert_rules())
        assert ev.worst_action({"unsafe_action_count": 1}) == ALERT_ACTION_STOP_NEW

    def test_severity_ordering(self):
        rules = [
            AlertRule("latency", "latency_p95_seconds", ">", 5.0, severity="P1"),
            AlertRule("unsafe", "unsafe_action_count", ">", 0.0, severity="P0"),
        ]
        ev = AlertEvaluator(rules=rules)
        firing = ev.evaluate({"latency_p95_seconds": 10.0, "unsafe_action_count": 1})
        assert firing[0].rule_name == "unsafe"  # P0 优先


# ═══════════════════════════════════════════════════════════════
# tool_harness
# ═══════════════════════════════════════════════════════════════


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolContract(
            name="search_knowledge",
            description="搜索产品知识",
            side_effect_level=SideEffectLevel.L1,
            retryable=True,
        )
    )
    reg.register(
        ToolContract(
            name="submit_pr",
            description="提交 PR",
            side_effect_level=SideEffectLevel.L2,
            idempotency_required=True,
            retryable=True,
        )
    )
    reg.register(
        ToolContract(
            name="delete_repo",
            description="删除仓库",
            side_effect_level=SideEffectLevel.L3,
        )
    )
    return reg


class TestToolContract:
    def test_retryable_requires_idempotent_for_l2(self):
        with pytest.raises(ValueError, match="retryable"):
            ToolContract(name="t", side_effect_level=SideEffectLevel.L2, retryable=True)

    def test_l1_retryable_ok(self):
        ToolContract(name="t", side_effect_level=SideEffectLevel.L1, retryable=True)

    def test_l2_idempotent_retryable_ok(self):
        ToolContract(name="t", side_effect_level=SideEffectLevel.L2, idempotency_required=True, retryable=True)


class TestToolRegistry:
    def test_register_and_get(self):
        reg = _registry()
        assert "search_knowledge" in reg
        assert reg.get("search_knowledge").side_effect_level == SideEffectLevel.L1
        assert reg.names() == ["delete_repo", "search_knowledge", "submit_pr"]

    def test_duplicate_rejected(self):
        reg = _registry()
        with pytest.raises(ToolRegistryError, match="duplicate"):
            reg.register(ToolContract(name="search_knowledge"))

    def test_unknown_get(self):
        with pytest.raises(ToolRegistryError, match="unknown"):
            _registry().get("ghost")

    def test_allowlist(self):
        reg = _registry()
        contracts = reg.allowlist(["search_knowledge", "submit_pr"])
        assert [c.name for c in contracts] == ["search_knowledge", "submit_pr"]

    def test_snapshot_fingerprint_stable_and_sensitive(self):
        reg = _registry()
        snap = reg.snapshot()
        assert snap["registry_version"] == "1.0"
        assert snap["fingerprint"].startswith("sha256:")
        assert _registry().snapshot()["fingerprint"] == snap["fingerprint"]
        reg2 = ToolRegistry()
        reg2.register(ToolContract(name="other_tool"))
        assert reg2.snapshot()["fingerprint"] != snap["fingerprint"]


class TestToolAdapters:
    def test_fake_adapter(self):
        adapter = FakeToolAdapter(results={"search": "结果A"})
        assert asyncio.run(adapter.invoke("search", {})) == "结果A"
        assert asyncio.run(adapter.invoke("other", {})) == "[fake:other]"

    def test_recorded_adapter(self):
        log = RecordedToolLog()
        log.record(tool_name="search", args_hash="sha256:aaa", result_hash="sha256:bbb", ok=True)
        adapter = RecordedToolAdapter(log)
        # args hash 需匹配，否则 KeyError
        with pytest.raises(KeyError):
            asyncio.run(adapter.invoke("search", {"q": "x"}))

    def test_sandbox_rejects_l3(self):
        adapter = SandboxToolAdapter(registry=_registry())
        with pytest.raises(PermissionError, match="L3"):
            asyncio.run(adapter.invoke("delete_repo", {}))
        assert asyncio.run(adapter.invoke("search_knowledge", {})) == "[sandbox:search_knowledge ok]"


class TestToolResultSanitizer:
    def test_truncation(self):
        s = ToolResultSanitizer(max_bytes=10)
        original = "中文内容" * 20
        res = s.sanitize(tool_name="t", raw_text=original)
        assert res.truncated
        assert len(res.text) < len(original)
        assert len(res.text.encode("utf-8")) <= 10 + 3  # 截断点可能落在多字节字符上

    def test_injection_detected(self):
        s = ToolResultSanitizer()
        res = s.sanitize(tool_name="t", raw_text="<script>alert(1)</script>")
        assert res.injection_detected
        assert "<script" in res.text

    def test_redaction(self):
        s = ToolResultSanitizer()
        res = s.sanitize(tool_name="t", raw_text="token=sk-abcdefgh12345678 联系 test@example.com")
        assert "redacted:sk" in res.text or "redacted" in res.text
        assert "test@example.com" not in res.text

    def test_clean_passthrough(self):
        s = ToolResultSanitizer()
        res = s.sanitize(tool_name="t", raw_text="普通结果")
        assert not res.truncated
        assert not res.injection_detected
        assert res.redacted_fields == []


class TestRecordedToolLog:
    def test_record_and_lookup(self):
        log = RecordedToolLog()
        log.record(tool_name="search", args_hash="sha256:a", result_hash="sha256:r", ok=True)
        entry = log.lookup(args_hash="sha256:a")
        assert entry is not None
        assert entry["result_hash"] == "sha256:r"
        assert log.lookup(args_hash="sha256:miss") is None
        assert log.entry_count() == 1

    def test_snapshot_roundtrip(self):
        log = RecordedToolLog()
        log.record(tool_name="search", args_hash="sha256:a", result_hash="sha256:r", ok=False)
        loaded = RecordedToolLog.load(log.snapshot())
        assert loaded.entry_count() == 1


class TestProtectedToolCaller:
    def _caller(self, **kw) -> ProtectedToolCaller:
        base = {"registry": _registry(), "adapter": FakeToolAdapter(results={"search_knowledge": "ok"})}
        base.update(kw)
        return ProtectedToolCaller(**base)

    def test_success_sanitized_and_recorded(self):
        caller = self._caller()
        log = RecordedToolLog()
        outcome = asyncio.run(caller.call("search_knowledge", {}, recorded=log))
        assert outcome.ok
        assert outcome.sanitized
        assert outcome.result_hash.startswith("sha256:")
        assert log.entry_count() == 1

    def test_breaker_open_short_circuits(self):
        reg = CircuitBreakerRegistry(CircuitConfig(failure_threshold=1))
        breaker = reg.breaker("tool:search_knowledge")
        breaker.record_failure()  # 打开熔断器
        caller = self._caller(breaker_registry=reg)
        outcome = asyncio.run(caller.call("search_knowledge", {}))
        assert not outcome.ok
        assert outcome.error_code == "breaker_open"

    def test_retry_on_failure_then_success(self):
        attempts = {"n": 0}

        class FlakyAdapter:
            kind = "fake"

            async def invoke(self, name, args):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise TimeoutError("timeout")
                return "recovered"

        contract = ToolContract(name="flaky", side_effect_level=SideEffectLevel.L1, retryable=True)
        reg = ToolRegistry()
        reg.register(contract)
        caller = ProtectedToolCaller(registry=reg, adapter=FlakyAdapter(), max_retries=2, backoff_jitter=0.0)
        outcome = asyncio.run(caller.call("flaky", {}))
        assert outcome.ok
        assert attempts["n"] == 3

    def test_telemetry_recorded(self):
        metrics = MetricCollector()
        caller = self._caller(telemetry=metrics)
        asyncio.run(caller.call("search_knowledge", {}))
        assert metrics.counter("tool_calls", tool="search_knowledge", status="ok") == 1


# ═══════════════════════════════════════════════════════════════
# model_harness
# ═══════════════════════════════════════════════════════════════


class TestMapModelError:
    def test_timeout(self):
        kind, _ = map_model_error(TimeoutError("timeout"))
        assert kind == ModelErrorKind.TIMEOUT

    def test_rate_limit_text(self):
        kind, _ = map_model_error(RuntimeError("429 Too Many Requests"))
        assert kind == ModelErrorKind.RATE_LIMIT

    def test_server_5xx(self):
        kind, _ = map_model_error(RuntimeError("500 Internal Server Error"))
        assert kind == ModelErrorKind.SERVER_ERROR

    def test_connection(self):
        kind, _ = map_model_error(RuntimeError("connection reset"))
        assert kind == ModelErrorKind.CONNECTION

    def test_auth(self):
        kind, _ = map_model_error(RuntimeError("unauthorized 401"))
        assert kind == ModelErrorKind.AUTH

    def test_unknown(self):
        kind, _ = map_model_error(RuntimeError("weird"))
        assert kind == ModelErrorKind.UNKNOWN


class TestModelRateLimiter:
    def _clock(self):
        state = {"t": 0.0}
        return lambda: state["t"]

    def test_cps_limit(self):
        now = self._clock()
        limiter = ModelRateLimiter(max_calls_per_second=1, now_provider=now)
        assert asyncio.run(limiter.acquire(model_id="m"))
        assert not asyncio.run(limiter.acquire(model_id="m"))  # 超过 1/s 拒绝

    def test_tpm_limit(self):
        now = self._clock()
        limiter = ModelRateLimiter(max_tokens_per_minute=100, now_provider=now)
        assert asyncio.run(limiter.acquire(model_id="m", estimated_input_tokens=60))
        assert not asyncio.run(limiter.acquire(model_id="m", estimated_input_tokens=60))  # 120 > 100

    def test_no_limit_always_allowed(self):
        limiter = ModelRateLimiter()
        for _ in range(5):
            assert asyncio.run(limiter.acquire(model_id="m"))


class TestModelHarness:
    def _harness(self, **kw) -> ModelHarness:
        base = {
            "allowlist": ["deepseek-chat", "deepseek-v4-flash"],
            "adapter": FakeModelAdapter(responses=["主回复", "备用回复"]),
        }
        base.update(kw)
        return ModelHarness(**base)

    def test_allowlist_rejects_unknown(self):
        h = self._harness()
        res = asyncio.run(h.generate([], model_id="gpt-4o"))
        assert not res.ok
        assert res.reason_code == "not_in_allowlist"

    def test_forced_model_ok(self):
        h = self._harness()
        res = asyncio.run(h.generate([], model_id="deepseek-chat"))
        assert res.ok
        assert res.model_id == "deepseek-chat"
        assert not res.degraded

    def test_fallback_on_failure(self):
        adapter = FakeModelAdapter(
            responses=["ok"],
            faults=[RuntimeError("429 Too Many Requests")],
        )
        h = self._harness(adapter=adapter)
        res = asyncio.run(h.generate([]))
        assert res.ok
        assert res.fallback_used
        assert res.degraded

    def test_all_fail_returns_last_error(self):
        adapter = FakeModelAdapter(
            responses=[],
            faults=[RuntimeError("server error"), RuntimeError("server error")],
        )
        h = self._harness(adapter=adapter)
        res = asyncio.run(h.generate([]))
        assert not res.ok
        assert res.error_kind == ModelErrorKind.SERVER_ERROR.value

    def test_no_fallback(self):
        adapter = FakeModelAdapter(faults=[RuntimeError("boom")])
        h = self._harness(adapter=adapter)
        res = asyncio.run(h.generate([], allow_fallback=False))
        assert not res.ok
        assert not res.fallback_used

    def test_breaker_open_skips_model(self):
        reg = CircuitBreakerRegistry(CircuitConfig(failure_threshold=1))
        reg.breaker("provider:deepseek-chat").record_failure()
        h = self._harness(breaker_registry=reg)
        res = asyncio.run(h.generate([], model_id="deepseek-chat"))
        assert not res.ok
        assert res.breaker_open

    def test_telemetry(self):
        metrics = MetricCollector()
        h = self._harness(telemetry=metrics)
        asyncio.run(h.generate([], model_id="deepseek-chat"))
        assert metrics.counter("model_calls", model="deepseek-chat", status="ok") == 1

    def test_usage_snapshot_passthrough(self):
        h = self._harness()
        res = asyncio.run(h.generate([], model_id="deepseek-chat"))
        assert res.usage is not None
        assert isinstance(res.usage, UsageSnapshot)
        assert res.usage.finish_reason == FinishReason.STOP


# ═══════════════════════════════════════════════════════════════
# context_harness
# ═══════════════════════════════════════════════════════════════


def _ctx_request(**kw) -> ContextRequest:
    base = {
        "purpose": "score",
        "user_id": "u1",
        "products": ["sec-pr"],
        "query": "如何评价某产品",
    }
    base.update(kw)
    return ContextRequest(**base)


def _ctx_sources() -> list[ContextSource]:
    return [
        ContextSource(
            source="policy:security",
            content="安全合规政策：不得泄露密钥",
            section_type="security_policy",
            required=True,
        ),
        ContextSource(
            source="knowledge:overview",
            content="产品知识：某产品支持告警分析。",
            section_type="required_product",
            product="sec-pr",
            published=True,
            required=True,
        ),
        ContextSource(
            source="memory:pref",
            content="用户偏好：喜欢简洁输出",
            section_type="memory_preference",
        ),
    ]


class TestContextHarness:
    def test_build_and_hash(self):
        h = ContextHarness()
        plan = h.build(_ctx_request(), _ctx_sources())
        assert plan.total_tokens > 0
        assert plan.plan_hash.startswith("sha256:")

    def test_reproducible(self):
        h = ContextHarness()
        report = h.verify_reproducible(_ctx_request(), _ctx_sources(), runs=3)
        assert report.stable
        assert len(report.observed_hashes) == 3

    def test_reproducible_changes_with_source(self):
        h = ContextHarness()
        a = h.verify_reproducible(_ctx_request(), _ctx_sources())
        other = [
            *_ctx_sources(),
            ContextSource(
                source="knowledge:extra",
                content="额外知识",
                section_type="required_product",
                product="sec-pr",
                required=True,
            ),
        ]
        b = h.verify_reproducible(_ctx_request(), other)
        assert a.plan_hash != b.plan_hash

    def test_diff_plans_added_removed(self):
        h = ContextHarness()
        before = h.build(_ctx_request(), _ctx_sources()[:2])
        after = h.build(_ctx_request(), _ctx_sources())
        diff = ContextHarness.diff_plans(before, after)
        assert diff.added == ["memory:pref"]
        assert diff.removed == []
        assert diff.hash_changed

    def test_diff_plans_removed(self):
        h = ContextHarness()
        before = h.build(_ctx_request(), _ctx_sources())
        after = h.build(_ctx_request(), _ctx_sources()[:1])
        diff = ContextHarness.diff_plans(before, after)
        assert diff.removed == ["knowledge:overview", "memory:pref"]

    def test_token_deviation(self):
        stats = ContextHarness.token_deviation(estimated_tokens=100, actual_tokens=120)
        assert stats.deviation_ratio == pytest.approx(0.2)
        under = ContextHarness.token_deviation(estimated_tokens=100, actual_tokens=80)
        assert under.deviation_ratio == pytest.approx(-0.2)

    def test_audit_plan(self):
        h = ContextHarness()
        plan = h.build(_ctx_request(), _ctx_sources())
        audit = ContextHarness.audit_plan(plan, manifest_hash="sha256:abc")
        assert audit.plan_hash == plan.plan_hash
        assert audit.total_tokens == plan.total_tokens
        assert audit.dropped == []


# ═══════════════════════════════════════════════════════════════
# eval_harness
# ═══════════════════════════════════════════════════════════════


def _eval_result(**kw) -> EvalResult:
    base = {
        "backend": "candidate",
        "terminal_status": "completed",
        "token_and_cost": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.0001},
        "latency_ms": 120.0,
    }
    base.update(kw)
    return EvalResult(**base)


def _eval_case() -> EvalCase:
    return EvalCase(
        case_id="case-1",
        dataset_version="real_v1",
        category="short_qa",
        input_fixture={"question": "某产品有哪些能力？"},
        tenant_fixture={"user_id": "eval-user"},
    )


class TestEvalSnapshot:
    def test_save_verify_roundtrip(self, tmp_path: Path):
        snap = EvalSnapshot(output_dir=tmp_path, runner_version="v2")
        path = snap.save(level="release", report={"passed": True}, dataset_version="real_v1")
        assert path.exists()
        payload = EvalSnapshot.load(path)
        assert EvalSnapshot.verify(dict(payload))  # verify 会 pop hash，传副本

    def test_verify_rejects_tampering(self, tmp_path: Path):
        snap = EvalSnapshot(output_dir=tmp_path)
        path = snap.save(level="pr", report={"passed": True})
        payload = EvalSnapshot.load(path)
        payload["report"]["passed"] = False
        assert not EvalSnapshot.verify(payload)


class TestMatrix:
    def test_aggregate_success_rate(self):
        cell = MatrixCell(label="deepseek-chat")
        pair_ok = PairedEvalResult(
            case_id="c1", category="x", legacy=_eval_result(), candidate=_eval_result()
        )
        pair_fail = PairedEvalResult(
            case_id="c2",
            category="x",
            legacy=_eval_result(),
            candidate=_eval_result(terminal_status="failed"),
        )
        agg = MatrixRunner._aggregate([pair_ok, pair_fail], cell)
        assert agg["success_rate"] == 0.5
        assert agg["cases"] == 2
        assert agg["avg_tokens"] == 150.0
        assert agg["p95_latency_ms"] == 120.0

    def test_best_by(self):
        report = MatrixReport(
            cells=[
                {"label": "a", "p95_latency_ms": 500.0, "success_rate": 0.9},
                {"label": "b", "p95_latency_ms": 300.0, "success_rate": 0.8},
            ]
        )
        best = report.best_by("p95_latency_ms")
        assert best["label"] == "b"


class TestMinimalRepro:
    def test_export(self, tmp_path: Path):
        repro = MinimalRepro(output_dir=tmp_path)
        result = _eval_result(terminal_status="failed", failure_attribution="loop_no_progress")
        path = repro.export(case=_eval_case(), result=result)
        assert path.exists()
        package = json.loads(path.read_text(encoding="utf-8"))
        assert package["case_id"] == "case-1"
        assert package["failure_attribution"] == "loop_no_progress"
        assert package["repro_hash"].startswith("sha256:")
        # 复现包不含 prompt 正文（安全约束）
        assert "question" not in json.dumps(package)


# ═══════════════════════════════════════════════════════════════
# fault_harness
# ═══════════════════════════════════════════════════════════════


class TestFaultInjector:
    def test_all_scenarios_registered(self):
        assert len(FAULT_SCENARIOS) == 11

    def test_unknown_scenario(self):
        with pytest.raises(ValueError, match="unknown fault scenario"):
            FaultInjector().register_scenario("ghost")

    def test_error_fault_raises(self):
        injector = FaultInjector()
        injector.register(FaultSpec("execute", FaultType.RATE_LIMIT_429, error_code="429"))
        with pytest.raises(FaultInjected) as exc:
            asyncio.run(injector.inject("execute"))
        assert exc.value.error_code == "429"

    def test_timeout_raises_timeouterror(self):
        injector = FaultInjector()
        injector.register(FaultSpec("execute", FaultType.TIMEOUT))
        with pytest.raises(TimeoutError):
            asyncio.run(injector.inject("execute"))

    def test_process_kill_raises_processkilled(self):
        injector = FaultInjector()
        injector.register(FaultSpec("checkpoint", FaultType.PROCESS_KILL))
        with pytest.raises(ProcessKilled):
            asyncio.run(injector.inject("checkpoint"))

    def test_marker_fault_returns_marker(self):
        injector = FaultInjector()
        injector.register(FaultSpec("checkpoint", FaultType.DUPLICATE_EVENT))
        assert asyncio.run(injector.inject("checkpoint")) == "duplicate_event"

    def test_disabled_injector_noop(self):
        injector = FaultInjector(enabled=False)
        injector.register(FaultSpec("execute", FaultType.EXCEPTION))
        assert asyncio.run(injector.inject("execute")) is None

    def test_hits_counting(self):
        injector = FaultInjector()
        injector.register(FaultSpec("execute", FaultType.EXCEPTION, repeat=2))
        with pytest.raises(FaultInjected):
            asyncio.run(injector.inject("execute"))
        assert injector.hits("execute") == 1
        assert injector.hits() == 1

    def test_repeat_exhausted(self):
        injector = FaultInjector()
        injector.register(FaultSpec("execute", FaultType.EXCEPTION, repeat=1))
        with pytest.raises(FaultInjected):
            asyncio.run(injector.inject("execute"))
        assert asyncio.run(injector.inject("execute")) is None  # 已触发完


class TestFaultDrillRunner:
    def _recovery_target(self, injector: FaultInjector):
        async def target(step: str) -> bool:
            marker = await injector.inject(step, context={"step": step})
            if marker == "duplicate_event":
                return True
            return True

        return target

    def test_run_passes_when_recovered(self):
        injector = FaultInjector()
        runner = FaultDrillRunner(injector=injector, now_provider=lambda: FIXED_NOW)
        report = asyncio.run(
            runner.run(scenario="duplicate_event", target=self._recovery_target(injector))
        )
        assert report.passed
        assert report.scenario == "duplicate_event"
        assert report.steps[0].outcome == "passed"

    def test_run_fails_when_target_crashes(self):
        injector = FaultInjector()
        injector.register_scenario("exception")  # 先注册故障场景

        async def bad_target(step: str) -> bool:
            await injector.inject(step)
            return False

        runner = FaultDrillRunner(injector=injector, now_provider=lambda: FIXED_NOW)
        report = asyncio.run(runner.run(scenario="exception", target=bad_target))
        assert not report.passed
        assert report.steps[0].outcome == "failed"
        assert report.steps[0].observed == "exception:internal_error"

    def test_report_to_legacy_dict(self):
        injector = FaultInjector()
        runner = FaultDrillRunner(injector=injector, now_provider=lambda: FIXED_NOW)
        report = asyncio.run(
            runner.run(scenario="log_failure", target=self._recovery_target(injector))
        )
        legacy = report.to_legacy_dict()
        assert legacy["scenario"] == "log_failure"
        assert "steps" in legacy

    def test_unknown_scenario(self):
        runner = FaultDrillRunner(injector=FaultInjector())
        with pytest.raises(ValueError):
            asyncio.run(runner.run(scenario="ghost", target=self._recovery_target(runner.injector)))


# ═══════════════════════════════════════════════════════════════
# rollout_controller
# ═══════════════════════════════════════════════════════════════


class TestRolloutTracker:
    def test_not_ready_below_sample_size(self):
        tracker = RolloutTracker(
            capability="chat_agent", min_sample_size=3, observation_window_seconds=3600
        )
        tracker.record(user_id="u1", outcome="success", ts=FIXED_NOW)
        tracker.record(user_id="u2", outcome="success", ts=FIXED_NOW)
        assert not tracker.ready_to_advance(now=FIXED_NOW + timedelta(hours=2))

    def test_not_ready_within_window(self):
        tracker = RolloutTracker(
            capability="chat_agent", min_sample_size=1, observation_window_seconds=3600
        )
        tracker.record(user_id="u1", outcome="success", ts=FIXED_NOW)
        assert not tracker.ready_to_advance(now=FIXED_NOW + timedelta(minutes=1))

    def test_ready_both_conditions(self):
        tracker = RolloutTracker(
            capability="chat_agent", min_sample_size=1, observation_window_seconds=3600
        )
        tracker.record(user_id="u1", outcome="success", ts=FIXED_NOW)
        assert tracker.ready_to_advance(now=FIXED_NOW + timedelta(hours=2))

    def test_success_rate(self):
        tracker = RolloutTracker(capability="chat_agent")
        tracker.record(user_id="u1", outcome="success", ts=FIXED_NOW)
        tracker.record(user_id="u2", outcome="fail", ts=FIXED_NOW)
        assert tracker.success_rate() == 0.5

    def test_advance_stages(self):
        tracker = RolloutTracker(capability="chat_agent", stage=RolloutStage.OFFLINE)
        assert tracker.next_stage() == RolloutStage.SHADOW
        assert tracker.advance(now=FIXED_NOW) == RolloutStage.SHADOW
        assert tracker.stage == RolloutStage.SHADOW
        # 推进到 FULL 后不再前进
        tracker.stage = RolloutStage.FULL
        assert tracker.advance() is None

    def test_snapshot(self):
        tracker = RolloutTracker(capability="chat_agent", min_sample_size=1)
        tracker.record(user_id="u1", outcome="success", ts=FIXED_NOW)
        snap = tracker.snapshot()
        assert snap["stage"] == "offline"
        assert snap["samples"] == 1


class TestRollbackController:
    def _controller(self) -> RollbackController:
        ev = AlertEvaluator(rules=default_alert_rules())
        return RollbackController(evaluator=ev, now_provider=lambda: FIXED_NOW)

    def test_no_alert_noop(self):
        controller = self._controller()
        decision = controller.evaluate({})
        assert decision.action == ACTION_NONE
        assert decision.reason_code == "no_alert"

    def test_latency_alert_rolls_back(self):
        controller = self._controller()
        decision = controller.evaluate({"latency_p95_seconds": 10.0})
        assert decision.action == ACTION_ROLLBACK
        assert decision.rule_name == "latency_p95_slo"

    def test_apply_rollback_only_closes_traffic(self):
        controller = self._controller()
        gate = FeatureGate(capability="chat_agent", percent=0.5, enabled=True)
        decision = controller.evaluate({"latency_p95_seconds": 10.0})
        controller.apply(decision, gate=gate)
        assert gate.percent == 0.0
        assert not gate.enabled
        assert len(controller.ledger.all()) == 1
        assert controller.ledger.entries_for("chat_agent")[0]["action"] == "rollback"

    def test_breaker_capability_action(self):
        reg = CircuitBreakerRegistry(CircuitConfig(failure_threshold=1))
        controller = RollbackController(
            evaluator=AlertEvaluator(rules=default_alert_rules(recovery_seconds=10.0)),
            breaker_registry=reg,
            now_provider=lambda: FIXED_NOW,
        )
        gate = FeatureGate(capability="autonomous", percent=0.1, enabled=True)
        decision = controller.evaluate({"stuck_running_seconds": 60.0})
        assert decision.action == ALERT_ACTION_BREAKER_CAPABILITY
        controller.apply(decision, gate=gate, capability="autonomous")
        assert not gate.enabled
        assert reg.breaker("capability:autonomous").state.value == "open"

    def test_stop_new_keeps_percent(self):
        controller = self._controller()
        gate = FeatureGate(capability="chat_agent", percent=0.5, enabled=True)
        decision = controller.evaluate({"unsafe_action_count": 1})
        assert decision.action == ALERT_ACTION_STOP_NEW
        controller.apply(decision, gate=gate)
        assert gate.percent == 0.5  # 只停新任务，不动已放量
        assert not gate.enabled


# ═══════════════════════════════════════════════════════════════
# capacity
# ═══════════════════════════════════════════════════════════════


class TestCapacityModel:
    def test_compute_fields(self):
        inputs = CapacityInputs(worker_concurrency=5, llm_calls_per_run=6)
        report = CapacityModel(inputs).compute()
        assert report.max_concurrent_runs == 4  # 5 * 0.8
        assert report.max_llm_calls_per_second > 0
        assert report.max_tokens_per_minute > 0
        assert report.max_tool_calls_per_second > 0
        assert report.queue_depth_threshold == 5
        assert report.usd_per_run > 0

    def test_rollout_cost_monotonic(self):
        inputs = CapacityInputs()
        costs = CapacityModel(inputs).compute().usd_per_day_by_rollout
        assert list(costs.keys()) == ["1%", "10%", "50%", "100%"]
        values = list(costs.values())
        assert values == sorted(values)
        assert costs["100%"] > 0

    def test_provider_cap_caps_throughput(self):
        inputs = CapacityInputs(provider_llm_calls_per_second=1.0)
        report = CapacityModel(inputs).compute()
        assert report.max_llm_calls_per_second <= 1.0

    def test_invalid_safety_factor(self):
        with pytest.raises(ValueError):
            CapacityInputs(safety_factor=0.0)

    def test_scenario_presets(self):
        assert set(SCENARIO_PRESETS) == {"short_qa", "high_context", "multi_tool", "multi_agent"}


class TestLoadSimulation:
    def test_invalid_arrival(self):
        with pytest.raises(ValueError, match="arrival_rps"):
            LoadScenario(arrival_rps=0.0)

    def test_under_load_no_saturation(self):
        # 服务时长 ~0.7s：10s 窗口内能完整跑完多个 run
        inputs = CapacityInputs(
            worker_concurrency=8,
            llm_calls_per_run=1,
            llm_p95_latency_ms=200,
            tool_calls_per_run=0,
        )
        sim = run_load_simulation(
            inputs,
            LoadScenario(arrival_rps=0.5, duration_seconds=10.0, seed=1),
        )
        assert sim.total_arrivals > 0
        assert sim.served > 0
        assert sim.success_rate == 1.0
        assert sim.utilization_ratio <= 1.0

    def test_overload_queues_or_rejects(self):
        inputs = CapacityInputs(worker_concurrency=1, llm_calls_per_run=4)
        sim = run_load_simulation(
            inputs,
            LoadScenario(arrival_rps=10.0, duration_seconds=20.0, seed=7),
        )
        # 到达率远超可持续吞吐 → 出现排队饱和或拒绝
        assert sim.peak_queue_depth > 0 or sim.rejected > 0
        assert sim.saturation_seconds > 0

    def test_failure_ratio_drops_success_rate(self):
        inputs = CapacityInputs(worker_concurrency=4, llm_calls_per_run=2)
        sim = run_load_simulation(
            inputs,
            LoadScenario(arrival_rps=1.0, duration_seconds=15.0, failure_ratio=0.5, seed=3),
        )
        assert sim.failed > 0
        assert sim.success_rate < 1.0

    def test_deterministic_with_seed(self):
        inputs = CapacityInputs()
        a = run_load_simulation(inputs, LoadScenario(arrival_rps=2.0, duration_seconds=5.0, seed=42))
        b = run_load_simulation(inputs, LoadScenario(arrival_rps=2.0, duration_seconds=5.0, seed=42))
        assert a.served == b.served
        assert a.rejected == b.rejected

    def test_report_legacy_dict(self):
        inputs = CapacityInputs()
        sim = run_load_simulation(inputs, LoadScenario(arrival_rps=0.5, duration_seconds=5.0, seed=1))
        legacy = sim.to_legacy_dict()
        assert "peak_queue_depth" in legacy
        assert "success_rate" in legacy
