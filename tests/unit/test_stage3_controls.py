"""阶段3 可控追溯与错误恢复 — 单元测试（WBS 3.1-3.8）。

覆盖：
  - 3.1 RunManifest（冻结/指纹/校验/持久化）
  - 3.2 统一追溯 trace（7 问回答）
  - 3.6 RecoveryPolicy + ErrorTaxonomy（错误分类 → 策略决策）
  - §4 CircuitBreaker（closed/open/half-open/fallback/隔离）
  - 3.7 Outbox（幂等/投递/死信/对账）
  - 3.8 审批 RBAC（职责分离/L3 双人/审计）
  - 3.5 RunLease + Reaper（心跳/接管/stale 扫描）
  - 3.3 三种重放（trace/candidate/recovery）
  - 3.4 DurableRunExecutor（租约/心跳/终态化/recover_stale）
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from agent.approval_rbac import (
    ApprovalAuditLog,
    DualApprovalState,
    audit_record,
    can_approve,
)
from agent.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitConfig,
    CircuitOpenError,
    CircuitState,
)
from agent.error_taxonomy import (
    DEFAULT_STRATEGY,
    ErrorCategory,
    classify_error,
    default_strategy,
)
from agent.outbox import EventOutbox, OutboxStore
from agent.policy_engine import PendingApproval
from agent.recovery_policy import (
    VALID_ACTIONS,
    RecoveryAction,
    RecoveryContext,
    RecoveryDecision,
    RecoveryPolicy,
)
from agent.replay import (
    candidate_replay,
    recovery_replay,
    trace_replay,
)
from agent.run_lease import LeaseConflictError, RunLeaseStore, RunReaper
from agent.run_manifest import (
    ManifestError,
    RunManifestStore,
    build_run_manifest,
    manifest_fingerprint,
    validate_manifest,
)
from agent.run_worker import DurableRunExecutor
from agent.runtime_state import (
    BudgetUsage,
    DecisionSummary,
    EvidenceRecord,
    RunBudget,
    RuntimeState,
    RuntimeStatus,
    ToolResultRecord,
)
from agent.trace import TRACE_QUESTIONS, build_trace

# 相对当前时间（避免硬编码日期过期导致 lease/trace 时间断言漂移）
FIXED_NOW = datetime.now(UTC)


# ═══════════════════════════════════════════════════════════════
# Fake Mongo（支持本测试所需的 insert/replace/update/delete/find/count）
# ═══════════════════════════════════════════════════════════════


def _match(doc: dict, query: dict) -> bool:
    for k, v in (query or {}).items():
        if isinstance(v, dict) and any(op.startswith("$") for op in v):
            if "$gt" in v and not (doc.get(k, 0) > v["$gt"]):
                return False
            if "$lt" in v and not (doc.get(k, 0) < v["$lt"]):
                return False
            if "$in" in v and doc.get(k) not in v["$in"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _FakeCursor:
    def __init__(self, col, query):
        self.col = col
        self.query = query
        self._sort_key: str | None = None
        self._sort_reverse: bool = False
        self._limit: int = 1000

    def sort(self, key, direction):
        self._sort_key = key
        self._sort_reverse = direction < 0
        return self

    def limit(self, n):
        self._limit = n
        return self

    async def to_list(self, length: int | None = None):
        limit = length or self._limit
        matched = [d for d in self.col.docs if _match(d, self.query)]
        if self._sort_key:
            matched = sorted(
                matched, key=lambda d: d.get(self._sort_key, 0), reverse=self._sort_reverse
            )
        return matched[:limit]


class _FakeCol:
    def __init__(self):
        self.docs: list[dict] = []
        self.created_indexes: list = []

    async def insert_one(self, doc: dict):
        self.docs.append(doc)

    async def replace_one(self, query, doc, upsert=False):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                self.docs[i] = doc
                return SimpleNamespace(matched_count=1)
        if upsert:
            self.docs.append(doc)
            return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def update_one(self, query, update, **kwargs):
        for d in self.docs:
            if _match(d, query):
                for op, fields in update.items():
                    if op == "$set":
                        d.update(fields)
                    elif op == "$inc":
                        for k, v in fields.items():
                            d[k] = d.get(k, 0) + v
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _match(d, query):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def find_one(self, query=None, sort=None):
        query = query or {}
        if sort and sort[0][0] == "sequence":
            matched = [d for d in self.docs if _match(d, query)]
            if not matched:
                return None
            return max(matched, key=lambda d: d.get("sequence", 0))
        for d in self.docs:
            if _match(d, query):
                return d
        return None

    def find(self, *args, **kwargs):
        query = kwargs.get("filter", args[0] if args else {})
        return _FakeCursor(self, query)

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _match(d, query or {}))

    async def create_indexes(self, indexes):
        self.created_indexes = list(indexes)
        return [i.document["name"] for i in indexes]


class _FakeDB(dict):
    def __init__(self):
        super().__init__()
        self._cols: dict[str, _FakeCol] = {}

    def __getitem__(self, name: str):
        if name not in self._cols:
            self._cols[name] = _FakeCol()
        return self._cols[name]


def _state(**overrides) -> RuntimeState:
    base = {
        "run_id": "run-1",
        "user_id": "u1",
        "goal": "完成一个测试目标",
        "acceptance_criteria": ["完成一个动作"],
        "budget": RunBudget(max_steps=10),
        "usage": BudgetUsage(started_at=FIXED_NOW, last_action_at=FIXED_NOW),
        "completed_steps": ["s1"],
        "pending_steps": ["s2"],
        "decision_summaries": [
            DecisionSummary(
                step_id="s1",
                phase="execute",
                action="retrieve_articles",
                tool_name="retrieve_articles",
                outcome="success",
                reason="ok",
                created_at=FIXED_NOW,
            )
        ],
        "evidence": [
            EvidenceRecord(
                evidence_id="ev-1", step_id="s1", acceptance_index=0, created_at=FIXED_NOW
            )
        ],
        "tool_results": [
            ToolResultRecord(
                tool_id="t1",
                ok=True,
                args_hash="sha256:x",
                result_hash="sha256:y",
                idempotency_key="ik-1",
                created_at=FIXED_NOW,
            )
        ],
    }
    base.update(overrides)
    return RuntimeState(**base)


# ═══════════════════════════════════════════════════════════════
# 3.1 RunManifest
# ═══════════════════════════════════════════════════════════════


class TestRunManifest:
    def _manifest(self, **kw) -> RuntimeState:
        base = {
            "run_id": "run-1",
            "user_id": "u1",
            "code_revision": "abc1234",
            "model_id": "deepseek-chat",
            "tool_registry_version": "v2",
            "budget": RunBudget(max_steps=10),
            "acceptance_criteria": ["完成一个动作"],
        }
        base.update(kw)
        return build_run_manifest(**base)

    def test_frozen_immutable(self):
        m = self._manifest()
        # pydantic v2 frozen 模型拒绝赋值会抛 ValidationError（ValueError 子类）
        with pytest.raises(ValueError):
            m.model_id = "other"  # frozen=True 拒绝修改

    def test_fingerprint_stable_and_sensitive(self):
        a = self._manifest()
        b = self._manifest()
        assert manifest_fingerprint(a) == manifest_fingerprint(b)
        c = self._manifest(model_id="other-model")
        assert manifest_fingerprint(c) != manifest_fingerprint(a)

    def test_validate_requires_code_revision(self):
        m = self._manifest(code_revision="")
        with pytest.raises(ManifestError, match="code_revision"):
            validate_manifest(m)

    def test_validate_requires_tool_registry_version(self):
        m = self._manifest(tool_registry_version="")
        with pytest.raises(ManifestError, match="tool_registry_version"):
            validate_manifest(m)

    def test_validate_requires_budget(self):
        m = self._manifest(budget=RunBudget(max_steps=0))
        with pytest.raises(ManifestError, match="预算"):
            validate_manifest(m)

    def test_validate_ok(self):
        validate_manifest(self._manifest())  # 不抛

    def test_execution_mode_coerced(self):
        m = self._manifest(execution_mode="shadow")
        assert m.execution_mode.value == "shadow"

    def test_store_roundtrip(self):
        store = RunManifestStore(_FakeDB())
        m = self._manifest()
        asyncio.run(store.save(m))
        loaded = asyncio.run(store.load("run-1"))
        assert loaded is not None
        assert loaded.model_id == "deepseek-chat"
        assert loaded.budget.max_steps == 10
        assert asyncio.run(store.load("missing")) is None


# ═══════════════════════════════════════════════════════════════
# 3.2 统一追溯
# ═══════════════════════════════════════════════════════════════


class TestTrace:
    def test_answers_all_seven_questions(self):
        state = _state(status=RuntimeStatus.COMPLETED)
        report = build_trace(state=state, created_at=FIXED_NOW)
        assert sorted(report.answers.keys()) == sorted(TRACE_QUESTIONS)
        assert "autonomous" in report.answers["mode_and_model"]
        assert report.answers["final_outcome"].startswith("已完成")
        assert report.evidence_coverage_ratio == 1.0
        assert report.covered_acceptance == [0]
        assert report.total_tokens == 0

    def test_manifest_merged_into_trace(self):
        from agent.run_manifest import build_run_manifest

        manifest = build_run_manifest(
            run_id="run-1",
            user_id="u1",
            code_revision="abc1234",
            model_id="deepseek-chat",
            tool_registry_version="v2",
            feature_flags={"rollout": "10%"},
        )
        report = build_trace(state=_state(), manifest=manifest, created_at=FIXED_NOW)
        assert report.manifest_saved
        assert report.model_id == "deepseek-chat"
        assert report.feature_flags == {"rollout": "10%"}
        assert report.manifest_fingerprint.startswith("sha256:")

    def test_waiting_approval_outcome(self):
        state = _state(status=RuntimeStatus.WAITING_APPROVAL)
        report = build_trace(state=state, created_at=FIXED_NOW)
        assert "等待审批" in report.answers["final_outcome"]

    def test_error_events_surface(self):
        state = _state()
        from agent.runtime_events import RuntimeEvent

        events = [
            RuntimeEvent(
                event_id="e1",
                sequence=1,
                run_id="run-1",
                event_type="step_failed",
                payload={"recovery_action": "retry_same"},
            )
        ]
        report = build_trace(state=state, runtime_events=events, created_at=FIXED_NOW)
        assert len(report.error_events) == 1
        assert report.recovery_decisions == ["retry_same"]


# ═══════════════════════════════════════════════════════════════
# 错误分类 + 3.6 RecoveryPolicy
# ═══════════════════════════════════════════════════════════════


class TestErrorTaxonomy:
    def test_rate_limit(self):
        assert classify_error("rate_limit") == ErrorCategory.TRANSIENT_PROVIDER

    def test_timeout(self):
        assert classify_error("timeout") == ErrorCategory.TRANSIENT_PROVIDER

    def test_5xx_server_error(self):
        assert classify_error("server_error") == ErrorCategory.TRANSIENT_PROVIDER

    def test_invalid_schema(self):
        assert classify_error("invalid_schema") == ErrorCategory.CONTRACT_ERROR

    def test_mongo_outage(self):
        assert classify_error("mongo_unavailable") == ErrorCategory.DEPENDENCY_OUTAGE

    def test_cas_conflict(self):
        assert classify_error("cas_conflict") == ErrorCategory.DATA_CONFLICT

    def test_budget(self):
        assert classify_error("budget_exceeded") == ErrorCategory.BUDGET_EXHAUSTED

    def test_loop(self):
        assert classify_error("loop_detected") == ErrorCategory.LOOP_NO_PROGRESS

    def test_unknown_falls_to_internal_bug(self):
        assert classify_error("") == ErrorCategory.INTERNAL_BUG

    def test_timeout_exc_type(self):
        assert classify_error("", exc=TimeoutError()) == ErrorCategory.TRANSIENT_PROVIDER

    def test_default_strategy_complete(self):
        for cat in ErrorCategory:
            assert cat in DEFAULT_STRATEGY
            assert default_strategy(cat) in VALID_ACTIONS


def _ctx(**kw) -> RecoveryContext:
    base = {
        "error_category": ErrorCategory.TRANSIENT_PROVIDER,
        "phase": "execute",
        "attempt": 1,
        "side_effect_level": "L1",
        "remaining_budget_ok": True,
    }
    base.update(kw)
    return RecoveryContext(**base)


class TestRecoveryPolicy:
    def test_transient_retry_then_switch_then_stop(self):
        policy = RecoveryPolicy()
        d1 = policy.decide(_ctx(attempt=1))
        assert d1.action == RecoveryAction.RETRY_SAME
        assert d1.max_attempts_left == 2
        d2 = policy.decide(_ctx(attempt=3, alternative_models=["model-b"]))
        assert d2.action == RecoveryAction.SWITCH_MODEL
        d3 = policy.decide(_ctx(attempt=3))
        assert d3.action == RecoveryAction.STOP_FAILED

    def test_transient_no_budget_continue_partial(self):
        d = RecoveryPolicy().decide(_ctx(remaining_budget_ok=False))
        assert d.action == RecoveryAction.CONTINUE_PARTIAL

    def test_contract_repair_once_then_stop(self):
        policy = RecoveryPolicy()
        d1 = policy.decide(_ctx(error_category=ErrorCategory.CONTRACT_ERROR, attempt=1))
        assert d1.action == RecoveryAction.REPAIR_THEN_RETRY
        d2 = policy.decide(_ctx(error_category=ErrorCategory.CONTRACT_ERROR, attempt=2))
        assert d2.action == RecoveryAction.STOP_FAILED

    def test_policy_denied_waits_approval(self):
        d = RecoveryPolicy().decide(
            _ctx(error_category=ErrorCategory.POLICY_DENIED, risk_level="L2")
        )
        assert d.action == RecoveryAction.WAIT_APPROVAL
        assert d.escalate_to_approval

    def test_policy_denied_low_risk_stops(self):
        d = RecoveryPolicy().decide(
            _ctx(error_category=ErrorCategory.POLICY_DENIED, risk_level="L0")
        )
        assert d.action == RecoveryAction.STOP_FAILED

    def test_budget_continue_partial_with_output(self):
        d = RecoveryPolicy().decide(
            _ctx(error_category=ErrorCategory.BUDGET_EXHAUSTED, completed_steps=3, evidence_count=2)
        )
        assert d.action == RecoveryAction.CONTINUE_PARTIAL

    def test_budget_stop_without_output(self):
        d = RecoveryPolicy().decide(_ctx(error_category=ErrorCategory.BUDGET_EXHAUSTED))
        assert d.action == RecoveryAction.STOP_FAILED

    def test_internal_bug_stops_no_retry(self):
        d = RecoveryPolicy().decide(_ctx(error_category=ErrorCategory.INTERNAL_BUG))
        assert d.action == RecoveryAction.STOP_FAILED
        assert "禁止盲重试" in d.reason

    def test_dependency_pause_then_dead_letter(self):
        policy = RecoveryPolicy()
        d1 = policy.decide(_ctx(error_category=ErrorCategory.DEPENDENCY_OUTAGE, attempt=1))
        assert d1.action == RecoveryAction.PAUSE_DEPENDENCY
        d2 = policy.decide(_ctx(error_category=ErrorCategory.DEPENDENCY_OUTAGE, attempt=2))
        assert d2.action == RecoveryAction.DEAD_LETTER

    def test_tool_transient_idempotent_retry(self):
        d = RecoveryPolicy().decide(
            _ctx(error_category=ErrorCategory.TOOL_TRANSIENT, side_effect_level="L1", attempt=1)
        )
        assert d.action == RecoveryAction.RETRY_SAME

    def test_tool_transient_switch_tool(self):
        d = RecoveryPolicy().decide(
            _ctx(
                error_category=ErrorCategory.TOOL_TRANSIENT,
                side_effect_level="L2",
                attempt=3,
                alternative_tools=["tool-b"],
            )
        )
        assert d.action == RecoveryAction.SWITCH_TOOL

    def test_all_actions_in_whitelist(self):
        assert RecoveryPolicy().actions <= frozenset(VALID_ACTIONS)

    def test_decision_fields(self):
        d = RecoveryPolicy().decide(_ctx(attempt=1))
        assert isinstance(d, RecoveryDecision)
        assert d.max_attempts_left >= 0
        assert isinstance(d.escalate_to_approval, bool)


# ═══════════════════════════════════════════════════════════════
# §4 CircuitBreaker
# ═══════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    def _breaker(self, **kw) -> CircuitBreaker:
        base = {
            "failure_threshold": 3,
            "timeout_threshold": 2,
            "window_size": 10,
            "cooldown_seconds": 3600,
        }
        base.update(kw)
        return CircuitBreaker("tool:test", CircuitConfig(**base))

    def test_closed_allows_all(self):
        b = self._breaker()
        assert b.state == CircuitState.CLOSED
        assert b.allow_request()

    def test_open_after_failure_threshold(self):
        b = self._breaker()
        for _ in range(3):
            b.record_failure()
        assert b.state == CircuitState.OPEN
        assert not b.allow_request()

    def test_open_after_timeout_threshold(self):
        b = self._breaker()
        for _ in range(2):
            b.record_failure(timed_out=True)
        assert b.state == CircuitState.OPEN

    def test_half_open_probe_quota_and_recover(self):
        b = self._breaker(cooldown_seconds=0.0)  # 立即转 half-open
        for _ in range(3):
            b.record_failure()
        assert b.state == CircuitState.OPEN
        assert b.allow_request()  # 冷却后转 half-open，放行探测
        b.record_success()  # 探测成功 → closed
        assert b.state == CircuitState.CLOSED
        assert b.allow_request()

    def test_half_open_failure_reopens(self):
        b = self._breaker(cooldown_seconds=0.0)
        for _ in range(3):
            b.record_failure()
        assert b.allow_request()  # 探测
        b.record_failure()  # 探测失败 → 再次 open
        assert b.state == CircuitState.OPEN

    def test_fallback_on_open(self):
        b = self._breaker()
        for _ in range(3):
            b.record_failure()
        with pytest.raises(CircuitOpenError):
            asyncio.run(b.call(lambda: _never()))  # open 且无 fallback → 抛错
        value = asyncio.run(b.call(lambda: _never(), fallback=lambda key: f"fallback-{key}"))
        assert value == "fallback-tool:test"

    def test_registry_isolation(self):
        reg = CircuitBreakerRegistry(CircuitConfig(failure_threshold=2))
        provider = reg.breaker("provider:deepseek")
        tool = reg.breaker("tool:search")
        provider.record_failure()
        provider.record_failure()
        assert provider.state == CircuitState.OPEN
        assert tool.state == CircuitState.CLOSED  # 隔离

    def test_protected_call_success(self):
        async def ok():
            return "result"

        b = self._breaker()
        value = asyncio.run(b.call(ok))
        assert value == "result"
        assert b.snapshot().success_count == 1

    def test_protected_call_failure_fallback(self):
        async def boom():
            raise TimeoutError("timeout")

        b = self._breaker(timeout_threshold=1)
        value = asyncio.run(b.call(boom, fallback=lambda key: "fb"))
        assert value == "fb"
        assert b.snapshot().timeout_count == 1


def _never():
    raise AssertionError("should not be called")


# ═══════════════════════════════════════════════════════════════
# 3.7 Outbox
# ═══════════════════════════════════════════════════════════════


class TestOutbox:
    def _store(self, max_attempts: int = 2) -> OutboxStore:
        return OutboxStore(_FakeDB(), max_attempts=max_attempts)

    def test_enqueue_and_claim(self):
        store = self._store()
        entry = asyncio.run(
            store.enqueue(run_id="run-1", event_type="state_transition", payload={"status": "ok"})
        )
        assert entry is not None
        claimed = asyncio.run(store.claim_next())
        assert len(claimed) == 1
        assert claimed[0].entry_id == entry.entry_id
        assert claimed[0].payload_hash.startswith("sha256:")

    def test_dedup_rejects_duplicate(self):
        store = self._store()
        first = asyncio.run(
            store.enqueue(run_id="run-1", event_type="run_state", dedup_key="run-1:snapshot")
        )
        second = asyncio.run(
            store.enqueue(run_id="run-1", event_type="run_state", dedup_key="run-1:snapshot")
        )
        assert first is not None
        assert second is None  # 重复投递幂等拒绝 → 0 重复副作用

    def test_mark_sent(self):
        store = self._store()
        entry = asyncio.run(store.enqueue(run_id="r1", event_type="e"))
        assert asyncio.run(store.mark_sent(entry.entry_id))
        assert asyncio.run(store.pending_count()) == 0

    def test_failed_dead_letters_after_max_attempts(self):
        store = self._store(max_attempts=2)
        entry = asyncio.run(store.enqueue(run_id="r1", event_type="e"))
        asyncio.run(store.mark_failed(entry.entry_id, error="boom"))
        # 第一次失败后仍可领取（attempts=1 < 2）
        assert asyncio.run(store.claim_next())
        asyncio.run(store.mark_failed(entry.entry_id, error="boom"))
        # 第二次失败：不再可领取，进入独立 dead-letter
        assert asyncio.run(store.claim_next()) == []
        dead = store.db["event_outbox_dead_letter"].docs
        assert len(dead) == 1
        assert dead[0]["entry_id"] == entry.entry_id

    def test_reconcile_delivers(self):
        store = self._store()
        asyncio.run(store.enqueue(run_id="r1", event_type="e", payload={"a": 1}))
        asyncio.run(store.enqueue(run_id="r1", event_type="e2", payload={"a": 2}))

        async def deliver(entry):
            return entry.payload.get("a") == 1  # 第一条成功，第二条失败

        stats = asyncio.run(store.reconcile(deliver=deliver))
        assert stats["sent"] == 1
        assert stats["failed"] == 1

    def test_event_outbox_flush(self):
        store = self._store()
        delivered: list[str] = []

        async def deliver(entry):
            delivered.append(entry.event_type)
            return True

        outbox = EventOutbox(store, deliver=deliver)
        asyncio.run(outbox.enqueue_run_event(run_id="r1", event_type="loop_started"))
        asyncio.run(outbox.enqueue_run_event(run_id="r1", event_type="loop_ended"))
        stats = asyncio.run(outbox.flush())
        assert stats["sent"] == 2
        assert delivered == ["loop_started", "loop_ended"]


# ═══════════════════════════════════════════════════════════════
# 3.8 审批 RBAC
# ═══════════════════════════════════════════════════════════════


def _approval(**kw) -> PendingApproval:
    base = {
        "approval_id": "ap-1",
        "action": "submit_pr",
        "risk_level": "L2",
        "params_hash": "sha256:abc",
        "params_summary": "repo=pr-agent",
        "status": "pending",
    }
    base.update(kw)
    return PendingApproval(**base)


class TestApprovalRbac:
    def test_self_approval_denied(self):
        res = can_approve(_approval(), initiator="alice", approver="alice")
        assert not res.allowed
        assert res.reason_code == "self_approval_denied"

    def test_l3_requires_admin(self):
        res = can_approve(_approval(risk_level="L3"), initiator="alice", approver="bob")
        assert not res.allowed
        assert res.requires_dual
        assert res.reason_code == "l3_requires_admin"

    def test_l3_admin_allowed(self):
        res = can_approve(
            _approval(risk_level="L3"), initiator="alice", approver="admin1", role="admin"
        )
        assert res.allowed

    def test_l2_single_ok(self):
        res = can_approve(_approval(risk_level="L2"), initiator="alice", approver="bob")
        assert res.allowed
        assert not res.requires_dual

    def test_missing_approver(self):
        res = can_approve(_approval(), initiator="alice", approver="")
        assert not res.allowed

    def test_dual_approval_two_approvers(self):
        dual = DualApprovalState(approval_id="ap-1")
        assert dual.add("bob")
        assert not dual.completed
        assert dual.add("bob") is False  # 同一人不可重复
        assert dual.add("carol")
        assert dual.completed
        assert dual.current == 2

    def test_audit_record_shape(self):
        rec = audit_record(
            approval_id="ap-1",
            run_id="run-1",
            actor="bob",
            action="approved",
            reason="ok",
            params_hash="sha256:abc",
        )
        assert rec["action"] == "approved"
        assert rec["actor"] == "bob"
        assert rec["schema_version"] == "1.0"

    def test_audit_log_store(self):
        log = ApprovalAuditLog(_FakeDB())
        asyncio.run(
            log.record(record=audit_record(approval_id="ap-1", actor="bob", action="rejected"))
        )
        entries = asyncio.run(log.list_for_approval("ap-1"))
        assert len(entries) == 1
        assert entries[0]["actor"] == "bob"


# ═══════════════════════════════════════════════════════════════
# 3.5 RunLease + Reaper
# ═══════════════════════════════════════════════════════════════


class TestRunLease:
    def _store(self, ttl: int = 120) -> RunLeaseStore:
        return RunLeaseStore(_FakeDB(), ttl_seconds=ttl)

    def test_acquire_new(self):
        store = self._store()
        lease = asyncio.run(store.acquire("run-1", "w1", now=FIXED_NOW))
        assert lease.owner_id == "w1"
        assert lease.fencing_token == 1
        assert not lease.expired

    def test_acquire_conflict(self):
        store = self._store()
        asyncio.run(store.acquire("run-1", "w1", now=FIXED_NOW))
        with pytest.raises(LeaseConflictError):
            asyncio.run(store.acquire("run-1", "w2", now=FIXED_NOW))

    def test_acquire_expired_takeover_increments_fencing(self):
        store = self._store()
        asyncio.run(store.acquire("run-1", "w1", now=FIXED_NOW))
        # 租约过期 → w2 可抢占，fencing token 递增（拒绝 w1 迟到写入）
        takeover = asyncio.run(store.acquire("run-1", "w2", now=FIXED_NOW + timedelta(seconds=300)))
        assert takeover.owner_id == "w2"
        assert takeover.fencing_token == 2

    def test_renew_matches_owner_and_fencing(self):
        store = self._store()
        lease = asyncio.run(store.acquire("run-1", "w1", now=FIXED_NOW))
        renewed = asyncio.run(
            store.renew("run-1", "w1", lease.fencing_token, now=FIXED_NOW + timedelta(seconds=60))
        )
        assert renewed is not None
        assert renewed.expires_at > lease.expires_at
        # 错误 fencing token → 拒绝
        assert asyncio.run(store.renew("run-1", "w1", 99)) is None

    def test_release_only_by_owner(self):
        store = self._store()
        lease = asyncio.run(store.acquire("run-1", "w1", now=FIXED_NOW))
        assert asyncio.run(store.release("run-1", "w1", lease.fencing_token))
        assert asyncio.run(store.load("run-1")) is None

    def test_reaper_reaps_stale_running(self):
        db = _FakeDB()
        store = RunLeaseStore(db)
        state_store = _store_like(db)
        # running 但租约已过期
        asyncio.run(state_store.save(_state(status=RuntimeStatus.RUNNING)))
        asyncio.run(store.acquire("run-1", "dead-worker", now=FIXED_NOW))
        reaper = RunReaper(state_store, store)
        reaped = asyncio.run(reaper.scan_stale_running(now=FIXED_NOW + timedelta(seconds=300)))
        assert len(reaped) == 1
        assert reaped[0]["run_id"] == "run-1"
        loaded = asyncio.run(state_store.load("run-1"))
        assert loaded.status == RuntimeStatus.STOPPED
        assert loaded.reason_code == "lease_expired"

    def test_reaper_skips_healthy_lease(self):
        db = _FakeDB()
        store = RunLeaseStore(db)
        state_store = _store_like(db)
        asyncio.run(state_store.save(_state(status=RuntimeStatus.RUNNING)))
        asyncio.run(store.acquire("run-1", "w1", now=FIXED_NOW))
        reaper = RunReaper(state_store, store)
        reaped = asyncio.run(reaper.scan_stale_running(now=FIXED_NOW))
        assert reaped == []
        loaded = asyncio.run(state_store.load("run-1"))
        assert loaded.status == RuntimeStatus.RUNNING  # 健康租约不被收割


def _store_like(db) -> _StateStoreShim:
    return _StateStoreShim(db)


class _StateStoreShim:
    """测试用：直接映射 runtime_runs 集合（复用 _FakeDB 结构）。"""

    def __init__(self, db):
        self.db = db
        self.col = db["runtime_runs"]

    async def save(self, state):
        doc = state.model_dump(mode="json")
        await self.col.replace_one({"run_id": state.run_id}, doc, upsert=True)

    async def load(self, run_id):
        doc = await self.col.find_one({"run_id": run_id})
        if doc is None:
            return None
        from agent.runtime_state import migrate_runtime_state

        return migrate_runtime_state(doc)

    async def list_runs(self, *, user_id: str = "", status: str = "", limit: int = 100):
        from agent.runtime_state import migrate_runtime_state

        return [migrate_runtime_state(d) for d in self.col.docs[:limit]]


# ═══════════════════════════════════════════════════════════════
# 3.3 三种重放
# ═══════════════════════════════════════════════════════════════


class TestReplay:
    def test_trace_valid(self):
        state = _state()
        result = trace_replay(state)
        assert result.valid
        assert result.steps_replayed == 1
        assert result.terminal_status == RuntimeStatus.PENDING.value

    def test_trace_violation_unknown_evidence_step(self):
        state = _state(
            evidence=[EvidenceRecord(evidence_id="ev-x", step_id="ghost-step", acceptance_index=0)]
        )
        result = trace_replay(state)
        assert not result.valid
        assert any("未知步骤" in v for v in result.violations)

    def test_trace_invalid_transition_after_terminal(self):
        state = _state()
        transitions = [
            {"to_status": "completed"},
            {"to_status": "running"},  # 终态后继续 → 违规
        ]
        result = trace_replay(state, transition_events=transitions)
        assert not result.valid

    def test_candidate_match(self):
        async def a(q):
            return "确定性答案A"

        result = asyncio.run(
            candidate_replay(inputs=["q1", "q2"], backend_a=a, backend_b=a, n_runs=2)
        )
        assert result.consistent
        assert result.match_ratio == 1.0

    def test_candidate_mismatch(self):
        async def a(q):
            return "答案A"

        async def b(q):
            return "答案B"

        result = asyncio.run(candidate_replay(inputs=["q1"], backend_a=a, backend_b=b))
        assert not result.consistent
        assert result.match_ratio == 0.0

    def test_recovery_terminal_skipped(self):
        store = _state_store_shim_runs(_FakeDB())
        state = _state(status=RuntimeStatus.COMPLETED)
        asyncio.run(store.save(state))
        result = asyncio.run(recovery_replay(runtime=None, state_store=store, run_id="run-1"))
        assert not result.executed
        assert result.status == "completed"

    def test_recovery_lease_conflict(self):
        db = _FakeDB()
        store = _state_store_shim_runs(db)
        lease_store = RunLeaseStore(db)
        asyncio.run(store.save(_state(status=RuntimeStatus.RUNNING)))
        asyncio.run(lease_store.acquire("run-1", "other-worker", now=FIXED_NOW))
        result = asyncio.run(
            recovery_replay(
                runtime=None,
                state_store=store,
                run_id="run-1",
                lease_store=lease_store,
                owner_id="replay",
                now=FIXED_NOW,
            )
        )
        assert not result.executed
        assert result.lease_conflict

    def test_recovery_executes_from_checkpoint(self):
        from agent.agent_runtime import AgentRuntime
        from agent.autonomous_service import DemoExecutor, DemoPlanner
        from agent.goal_validator import GoalValidator
        from agent.policy_engine import PolicyEngine

        db = _FakeDB()
        store = _state_store_shim_runs(db)
        lease_store = RunLeaseStore(db)
        # retrieve_articles 已完成，从 classify_articles 继续（DemoPlanner 链须含已完成步骤）
        state = _state(completed_steps=["retrieve_articles"], pending_steps=["classify_articles"])
        asyncio.run(store.save(state))

        runtime = AgentRuntime(
            planner=DemoPlanner(chain=["retrieve_articles", "classify_articles"]),
            executor=DemoExecutor(),
            policy=PolicyEngine(),
            goal_validator=GoalValidator(
                required_artifact_keys=(), high_risk_requires_confirm=False
            ),
            checkpointer=store.save,
            max_retries=2,
            backoff_jitter=0.0,
        )
        result = asyncio.run(
            recovery_replay(
                runtime=runtime,
                state_store=store,
                run_id="run-1",
                lease_store=lease_store,
                owner_id="replay",
                now=FIXED_NOW,
            )
        )
        assert result.executed
        assert result.status == RuntimeStatus.COMPLETED.value
        assert result.lease_ok

    def test_recovery_reports_missing_idempotency(self):
        store = _state_store_shim_runs(_FakeDB())
        state = _state(
            tool_results=[
                ToolResultRecord(
                    tool_id="t-write",
                    ok=True,
                    error_code="",
                    idempotency_key="",
                    created_at=FIXED_NOW,
                )
            ]
        )
        asyncio.run(store.save(state))
        result = asyncio.run(recovery_replay(runtime=None, state_store=store, run_id="run-1"))
        assert "t-write" in result.idempotency_missing


def _state_store_shim_runs(db) -> _StateStoreShim:
    return _StateStoreShim(db)


# ═══════════════════════════════════════════════════════════════
# 3.4 DurableRunExecutor（Autonomous 队列化）
# ═══════════════════════════════════════════════════════════════


def _runtime_factory(state, checkpointer):
    from agent.agent_runtime import AgentRuntime
    from agent.autonomous_service import DemoExecutor, DemoPlanner
    from agent.goal_validator import GoalValidator
    from agent.policy_engine import PolicyEngine

    return AgentRuntime(
        # 恢复链 = 已完成 + 待执行：DemoPlanner 按 completed_steps 长度索引链，
        # 不含已完成步骤会被视为"计划已执行完毕"而立即停止
        planner=DemoPlanner(chain=[*state.completed_steps, *state.pending_steps]),
        executor=DemoExecutor(),
        policy=PolicyEngine(),
        goal_validator=GoalValidator(required_artifact_keys=(), high_risk_requires_confirm=False),
        checkpointer=checkpointer,
        max_retries=2,
        backoff_jitter=0.0,
    )


class TestDurableRunExecutor:
    def _executor(self) -> tuple[DurableRunExecutor, _FakeDB, _StateStoreShim, RunLeaseStore]:
        db = _FakeDB()
        state_store = _StateStoreShim(db)
        lease_store = RunLeaseStore(db, ttl_seconds=120)
        executor = DurableRunExecutor(
            state_store=state_store,
            lease_store=lease_store,
            runtime_factory=_runtime_factory,
            owner_id="worker",
        )
        return executor, db, state_store, lease_store

    def test_execute_completes_and_releases_lease(self):
        executor, _db, state_store, lease_store = self._executor()
        # retrieve_articles 已完成，恢复执行 classify_articles（真实工具名，可通过策略引擎）
        state = _state(completed_steps=["retrieve_articles"], pending_steps=["classify_articles"])
        asyncio.run(state_store.save(state))
        result = asyncio.run(executor.execute("run-1", "u1", now=FIXED_NOW))
        assert result.executed
        assert result.status == RuntimeStatus.COMPLETED.value
        assert asyncio.run(lease_store.load("run-1")) is None  # 租约已释放

    def test_execute_terminal_skipped(self):
        executor, _, state_store, _ = self._executor()
        asyncio.run(state_store.save(_state(status=RuntimeStatus.COMPLETED)))
        result = asyncio.run(executor.execute("run-1", "u1"))
        assert not result.executed
        assert result.status == "completed"

    def test_execute_lease_conflict(self):
        executor, _db, state_store, lease_store = self._executor()
        asyncio.run(state_store.save(_state(status=RuntimeStatus.RUNNING)))
        asyncio.run(lease_store.acquire("run-1", "other", now=FIXED_NOW))
        result = asyncio.run(executor.execute("run-1", "u1", now=FIXED_NOW))
        assert not result.executed
        assert result.lease_conflict

    def test_execute_unclassified_error_terminalized(self):
        executor, _db, state_store, _ = self._executor()

        async def boom_factory(state, checkpointer):
            from agent.agent_runtime import AgentRuntime
            from agent.goal_validator import GoalValidator
            from agent.policy_engine import PolicyEngine

            async def bad_planner(state):
                raise RuntimeError("internal crash")

            return AgentRuntime(
                planner=bad_planner,
                executor=None,
                policy=PolicyEngine(),
                goal_validator=GoalValidator(
                    required_artifact_keys=(), high_risk_requires_confirm=False
                ),
                checkpointer=checkpointer,
                max_retries=0,
                backoff_jitter=0.0,
            )

        executor.runtime_factory = boom_factory
        asyncio.run(state_store.save(_state(status=RuntimeStatus.RUNNING)))
        result = asyncio.run(executor.execute("run-1", "u1", now=FIXED_NOW))
        assert result.executed
        assert result.status == RuntimeStatus.FAILED.value
        assert result.reason == "internal_error"
        loaded = asyncio.run(state_store.load("run-1"))
        assert loaded.status == RuntimeStatus.FAILED  # 未遗留 running

    def test_recover_stale_runs(self):
        executor, _db, state_store, lease_store = self._executor()
        asyncio.run(state_store.save(_state(status=RuntimeStatus.RUNNING)))
        asyncio.run(lease_store.acquire("run-1", "dead-worker", now=FIXED_NOW))
        reaped = asyncio.run(executor.recover_stale(now=FIXED_NOW + timedelta(seconds=300)))
        assert len(reaped) == 1
        loaded = asyncio.run(state_store.load("run-1"))
        assert loaded.status == RuntimeStatus.STOPPED
