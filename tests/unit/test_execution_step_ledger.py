"""ExecutionStepLedger 单元测试 — 阶段三 Step 6。

覆盖：初始化/幂等、CAS 租约与 fencing、complete/fail/skip/cancel、
过期 running 接管、恢复流程（跳过成功/仅执行 pending/retryable）、
reconciliation（不一致入修复队列且不盲目重跑）、索引。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pymongo import ReturnDocument

from agent.execution_step_ledger import (
    COLLECTION,
    ExecutionStepLedger,
    LeaseConflictError,
    StepLedgerEntry,
)
from agent.plan_contracts import PlanStep, PipelinePlan
from agent.worker_registry import WorkerResult


# ═══════════════════════════════════════════════════════════════
# Fake MongoDB（Motor 风格异步接口）
# ═══════════════════════════════════════════════════════════════


def _match(doc: dict, query: dict) -> bool:
    for key, cond in query.items():
        value = doc.get(key)
        if isinstance(cond, dict):
            for op, operand in cond.items():
                if op == "$in":
                    if value not in operand:
                        return False
                elif op == "$lte":
                    if not (value is not None and value <= operand):
                        return False
                elif op == "$gte":
                    if not (value is not None and value >= operand):
                        return False
                elif op == "$ne":
                    if value == operand:
                        return False
                else:
                    raise AssertionError(f"unsupported operator: {op}")
        else:
            if value != cond:
                return False
    return True


class _FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    async def to_list(self, length: int | None = None) -> list[dict]:
        return list(self._docs) if length is None else list(self._docs[:length])

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeCol:
    def __init__(self):
        self.docs: list[dict] = []
        self.created_indexes: list[str] = []
        self._next_id = 0

    def find(self, query: dict):
        return _FakeCursor([dict(d) for d in self.docs if _match(d, query)])

    async def find_one(self, query: dict):
        for doc in self.docs:
            if _match(doc, query):
                return dict(doc)
        return None

    async def insert_one(self, doc: dict):
        doc = dict(doc)
        doc["_id"] = self._next_id
        self._next_id += 1
        self.docs.append(doc)
        return SimpleNamespace(acknowledged=True, inserted_id=doc["_id"])

    async def find_one_and_update(self, filter_query: dict, update: dict, return_document=None):
        for doc in self.docs:
            if _match(doc, filter_query):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                if return_document == ReturnDocument.AFTER:
                    return dict(doc)
                return dict(doc)
        return None

    async def update_many(self, filter_query: dict, update: dict):
        matched = 0
        for doc in self.docs:
            if _match(doc, filter_query):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                matched += 1
        return SimpleNamespace(matched_count=matched, modified_count=matched)

    async def update_one(self, filter_query: dict, update: dict):
        result = await self.find_one_and_update(
            filter_query, update, return_document=ReturnDocument.AFTER
        )
        if result is None:
            return SimpleNamespace(matched_count=0, modified_count=0)
        return SimpleNamespace(matched_count=1, modified_count=1)

    async def create_indexes(self, indexes):
        for index in indexes:
            name = index.document.get("name")
            if name not in self.created_indexes:
                self.created_indexes.append(name)
        return list(self.created_indexes)


class _FakeDB:
    def __init__(self):
        self._cols: dict[str, _FakeCol] = {}

    def __getitem__(self, name: str) -> _FakeCol:
        return self._cols.setdefault(name, _FakeCol())


def _db() -> tuple[_FakeDB, _FakeCol]:
    db = _FakeDB()
    return db, db[COLLECTION]


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _plan() -> PipelinePlan:
    steps = [
        PlanStep(step_id="crawl", worker="crawl", policy="required", timeout_s=600, max_attempts=3),
        PlanStep(
            step_id="score", worker="score", depends_on=["crawl"],
            policy="required", timeout_s=600, max_attempts=3,
        ),
        PlanStep(
            step_id="draft", worker="draft", depends_on=["score"],
            policy="required", timeout_s=600, max_attempts=3,
        ),
        PlanStep(
            step_id="review", worker="review", depends_on=["draft"],
            policy="required", timeout_s=600, max_attempts=3,
        ),
    ]
    return PipelinePlan(
        schema_version="1.0",
        plan_id="plan-1",
        run_id="run-1",
        planner_version="default-v1",
        input_snapshot_hash="sha256:abc",
        steps=steps,
        rationale_summary="",
    )


def _ok_result(step_id: str, worker: str, *, idem: str = "k", input_hash: str = "h") -> WorkerResult:
    return WorkerResult(
        step_id=step_id,
        worker=worker,
        idempotency_key=idem,
        input_hash=input_hash,
        result_hash="sha256:r",
        status="succeeded",
        attempt=1,
    )


def _ledger(db=None, **kwargs) -> ExecutionStepLedger:
    db = db or _FakeDB()
    return ExecutionStepLedger(db, **kwargs)


def _fixed_now() -> datetime:
    return datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


async def _populate(db: _FakeDB, plan: PipelinePlan) -> ExecutionStepLedger:
    ledger = _ledger(db)
    await ledger.init_run(plan)
    return ledger


# ═══════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════


class TestInitRun:
    async def test_creates_pending_entries(self):
        db, col = _db()
        ledger = _ledger(db)
        entries = await ledger.init_run(_plan())
        assert len(entries) == 4
        assert all(e.status == "pending" for e in entries)
        assert all(e.lease_owner == "" for e in entries)
        assert all(e.expires_at > _fixed_now() for e in entries)
        # 落入集合
        stored = await col.find_one({"run_id": "run-1", "step_id": "crawl"})
        assert stored["status"] == "pending"
        assert stored["plan_id"] == "plan-1"

    async def test_init_is_idempotent(self):
        db, col = _db()
        ledger = _ledger(db)
        await ledger.init_run(_plan())
        second = await ledger.init_run(_plan())
        assert second == []
        assert len(col.docs) == 4

    async def test_worker_and_step_id_bound(self):
        db, _ = _db()
        ledger = _ledger(db)
        entries = await ledger.init_run(_plan())
        by_id = {e.step_id: e for e in entries}
        assert by_id["crawl"].worker == "crawl"
        assert by_id["review"].worker == "review"


class TestCASLease:
    async def test_begin_attempt_claims_pending(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        now = _fixed_now()
        entry = await ledger.begin_attempt(
            run_id="run-1", step_id="crawl", owner_id="orch-1",
            attempt=1, idempotency_key="u:r:crawl:h", input_hash="h", now=now,
        )
        assert entry.status == "running"
        assert entry.lease_owner == "orch-1"
        assert entry.fencing_token == 1
        assert entry.attempt == 1
        assert entry.lease_expires_at == now + timedelta(seconds=120)
        assert entry.idempotency_key == "u:r:crawl:h"

    async def test_double_claim_rejected(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o1", attempt=1)
        with pytest.raises(LeaseConflictError):
            await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o2", attempt=1)

    async def test_complete_requires_owner_and_fencing(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o1", attempt=1)
        # 错误 owner → 拒绝
        with pytest.raises(LeaseConflictError):
            await ledger.complete(
                run_id="run-1", step_id="crawl", owner_id="o2", fencing_token=1,
                result=_ok_result("crawl", "crawl"),
            )
        # 错误 fencing → 拒绝
        with pytest.raises(LeaseConflictError):
            await ledger.complete(
                run_id="run-1", step_id="crawl", owner_id="o1", fencing_token=99,
                result=_ok_result("crawl", "crawl"),
            )
        # 正确 owner + token → 成功，租约释放
        entry = await ledger.complete(
            run_id="run-1", step_id="crawl", owner_id="o1", fencing_token=1,
            result=_ok_result("crawl", "crawl", idem="k1", input_hash="h1"),
        )
        assert entry.status == "succeeded"
        assert entry.lease_owner == ""
        assert entry.lease_expires_at is None
        assert entry.result_hash == "sha256:r"
        assert entry.idempotency_key == "k1"

    async def test_terminal_step_not_claimable(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o1", attempt=1)
        await ledger.complete(
            run_id="run-1", step_id="crawl", owner_id="o1", fencing_token=1,
            result=_ok_result("crawl", "crawl"),
        )
        with pytest.raises(LeaseConflictError):
            await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o1", attempt=2)

    async def test_failed_step_claimable_for_replay(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o1", attempt=1)
        await ledger.fail(
            run_id="run-1", step_id="crawl", owner_id="o1", fencing_token=1,
            status="dead_lettered", error_type="timeout", retryable=True,
        )
        # 人工重放：dead_lettered → 再次领取，fencing 递增
        entry = await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o1", attempt=2)
        assert entry.status == "running"
        assert entry.fencing_token == 2

    async def test_fail_sets_error_and_releases_lease(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o1", attempt=1)
        entry = await ledger.fail(
            run_id="run-1", step_id="crawl", owner_id="o1", fencing_token=1,
            status="dead_lettered", error_type="timeout", retryable=True,
            error_message="exhausted",
        )
        assert entry.status == "dead_lettered"
        assert entry.error_type == "timeout"
        assert entry.retryable is True
        assert entry.lease_owner == ""
        assert entry.finished_at is not None

    async def test_mark_dead_lettered_persists_recovery_hint(self):
        """Step 8：死信保存稳定错误码/attempt/result-hash 与恢复建议，不含全文。"""
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o1", attempt=3)
        entry = await ledger.mark_dead_lettered(
            run_id="run-1",
            step_id="crawl",
            error_type="timeout",
            error_message="exhausted after 3 attempts",
            result_hash="sha256:r",
            retryable=True,
            recovery_hint="retry via POST /api/pipeline/runs/run-1/steps/crawl/replay",
        )
        assert entry is not None
        assert entry.status == "dead_lettered"
        assert entry.attempt == 3
        assert entry.error_type == "timeout"
        assert entry.result_hash == "sha256:r"
        assert "replay" in entry.recovery_hint
        assert "prompt" not in entry.model_dump()
        # 再次死信升级是 no-op（终态不可重复升级）
        again = await ledger.mark_dead_lettered(
            run_id="run-1", step_id="crawl", error_type="timeout"
        )
        assert again is None

    async def test_skip(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        entry = await ledger.skip(run_id="run-1", step_id="crawl", reason="optional failure")
        assert entry.status == "skipped"
        assert entry.error_type == "optional failure"

    async def test_skip_terminal_rejected(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.skip(run_id="run-1", step_id="crawl")
        with pytest.raises(LeaseConflictError):
            await ledger.skip(run_id="run-1", step_id="crawl")

    async def test_cancel_run(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o1", attempt=1)
        await ledger.complete(
            run_id="run-1", step_id="crawl", owner_id="o1", fencing_token=1,
            result=_ok_result("crawl", "crawl"),
        )
        n = await ledger.cancel_run("run-1")
        assert n == 3  # score/draft/review 仍 pending/running
        steps = {e.step_id: e for e in await ledger.list_run_steps("run-1")}
        assert steps["crawl"].status == "succeeded"
        assert steps["score"].status == "canceled"
        # 幂等
        assert await ledger.cancel_run("run-1") == 0


class TestTakeover:
    async def test_takeover_expired_running(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        now = _fixed_now()
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="old", attempt=1, now=now)
        taken = await ledger.takeover_expired(run_id="run-1", owner_id="new", now=now + timedelta(seconds=300))
        assert len(taken) == 1
        entry = taken[0]
        assert entry.lease_owner == "new"
        assert entry.fencing_token == 2
        assert entry.attempt == 2

    async def test_non_expired_running_not_taken(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        now = _fixed_now()
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="old", attempt=1, now=now)
        taken = await ledger.takeover_expired(run_id="run-1", owner_id="new", now=now + timedelta(seconds=30))
        assert taken == []
        entry = await ledger.get_step("run-1", "crawl")
        assert entry.lease_owner == "old"

    async def test_takeover_is_single_winner(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        now = _fixed_now()
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="old", attempt=1, now=now)
        # 第一个接管者延长期限后，第二个接管者的 CAS 必须失败
        await ledger.takeover_expired(run_id="run-1", owner_id="new1", now=now + timedelta(seconds=300))
        again = await ledger.takeover_expired(run_id="run-1", owner_id="new2", now=now + timedelta(seconds=300))
        assert again == []
        entry = await ledger.get_step("run-1", "crawl")
        assert entry.lease_owner == "new1"

    async def test_old_worker_late_write_rejected_after_takeover(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        now = _fixed_now()
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="old", attempt=1, now=now)
        await ledger.takeover_expired(run_id="run-1", owner_id="new", now=now + timedelta(seconds=300))
        # 旧 Worker 用旧 fencing token 迟到提交 → 拒绝
        with pytest.raises(LeaseConflictError):
            await ledger.complete(
                run_id="run-1", step_id="crawl", owner_id="old", fencing_token=1,
                result=_ok_result("crawl", "crawl"),
            )


class TestRecovery:
    async def test_plan_missing_blocks_recovery(self):
        db, _ = _db()
        ledger = _ledger(db)

        async def load_plan(run_id):
            return None

        result = await ledger.recover_run(run_id="run-1", owner_id="o", load_plan=load_plan)
        assert result.ok is False
        assert result.to_execute == []
        assert "plan/input snapshot not found" in result.issues

    async def test_recovers_pending_skips_succeeded(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        # crawl 成功，score pending，draft skipped，review pending
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o", attempt=1)
        await ledger.complete(
            run_id="run-1", step_id="crawl", owner_id="o", fencing_token=1,
            result=_ok_result("crawl", "crawl"),
        )
        await ledger.skip(run_id="run-1", step_id="draft", reason="optional")

        async def load_plan(run_id):
            return _plan()

        result = await ledger.recover_run(run_id="run-1", owner_id="o", load_plan=load_plan)
        assert result.ok is True
        executed = {e.step_id for e in result.to_execute}
        assert "score" in executed
        assert "review" in executed
        assert "crawl" not in executed
        assert "draft" not in executed
        assert [e.step_id for e in result.skipped] == ["crawl"]

    async def test_recovers_takes_over_expired_running(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        now = _fixed_now()
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="dead", attempt=1, now=now)
        await ledger.begin_attempt(run_id="run-1", step_id="score", owner_id="dead", attempt=1, now=now)

        async def load_plan(run_id):
            return _plan()

        result = await ledger.recover_run(
            run_id="run-1", owner_id="new", load_plan=load_plan,
            now=now + timedelta(seconds=300),
        )
        assert len(result.taken_over) == 2
        assert all(e.lease_owner == "new" for e in result.taken_over)
        assert all(e.fencing_token == 2 for e in result.taken_over)
        assert {e.step_id for e in result.to_execute} == {"crawl", "score", "draft", "review"}

    async def test_artifact_mismatch_enqueues_repair(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o", attempt=1)
        await ledger.complete(
            run_id="run-1", step_id="crawl", owner_id="o", fencing_token=1,
            result=_ok_result("crawl", "crawl"),
        )

        async def load_plan(run_id):
            return _plan()

        async def verify_artifact(entry):
            return False

        result = await ledger.recover_run(
            run_id="run-1", owner_id="o", load_plan=load_plan,
            verify_artifact=verify_artifact,
        )
        assert result.skipped == []
        assert any("artifact mismatch" in issue for issue in result.issues)
        # 修复队列已入队
        repair = await db["ledger_repair_queue"].find_one({"run_id": "run-1", "step_id": "crawl"})
        assert repair is not None
        assert repair["issue_type"] == "artifact_mismatch"
        assert repair["severity"] == "manual_review"


class TestReconcile:
    async def test_missing_ledger_issue(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())

        async def load_plan(run_id):
            return _plan()

        async def read_checkpoint(run_id):
            return {"completed_steps": ["crawl", "extra-step"]}

        result = await ledger.reconcile(
            run_id="run-1", load_plan=load_plan, read_checkpoint=read_checkpoint
        )
        missing = [i for i in result.issues if i.issue_type == "missing_ledger"]
        assert [i.step_id for i in missing] == ["extra-step"]
        assert result.repair_enqueued == 1
        repair = await db["ledger_repair_queue"].find_one({"run_id": "run-1", "step_id": "extra-step"})
        assert repair["status"] == "open"

    async def test_succeeded_without_checkpoint_and_artifact_fails(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o", attempt=1)
        await ledger.complete(
            run_id="run-1", step_id="crawl", owner_id="o", fencing_token=1,
            result=_ok_result("crawl", "crawl"),
        )

        async def read_checkpoint(run_id):
            return {"completed_steps": ["score"]}

        async def verify_artifact(entry):
            return False

        result = await ledger.reconcile(
            run_id="run-1", read_checkpoint=read_checkpoint, verify_artifact=verify_artifact,
        )
        assert any(i.issue_type == "succeeded_without_checkpoint" for i in result.issues)

    async def test_running_stale_issue(self):
        db, _ = _db()
        ledger = _ledger(db, stale_running_grace_seconds=60)
        await _populate(db, _plan())
        now = _fixed_now()
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o", attempt=1, now=now)

        result = await ledger.reconcile(
            run_id="run-1",
            now=now + timedelta(seconds=61),
        )
        assert any(i.issue_type == "running_stale" for i in result.issues)
        # 不盲目重跑：状态未被修改
        entry = await ledger.get_step("run-1", "crawl")
        assert entry.status == "running"

    async def test_reconcile_does_not_mutate_terminal(self):
        db, _ = _db()
        ledger = await _populate(db, _plan())
        await ledger.begin_attempt(run_id="run-1", step_id="crawl", owner_id="o", attempt=1)
        await ledger.complete(
            run_id="run-1", step_id="crawl", owner_id="o", fencing_token=1,
            result=_ok_result("crawl", "crawl"),
        )

        async def load_plan(run_id):
            return _plan()

        result = await ledger.reconcile(run_id="run-1", load_plan=load_plan)
        assert result.repair_enqueued == 0
        entry = await ledger.get_step("run-1", "crawl")
        assert entry.status == "succeeded"


class TestEnsureIndexes:
    async def test_creates_expected_indexes(self):
        db, col = _db()
        ledger = _ledger(db)
        created = await ledger.ensure_indexes()
        names = set(created)
        assert "uq_step_ledger_run_step" in names
        assert "uq_step_ledger_idempotency" in names
        assert "idx_step_ledger_status_lease" in names
        assert "idx_step_ledger_plan_created" in names
        assert "idx_step_ledger_deadletter" in names
        assert "ttl_step_ledger_expires" in names
        # 修复队列索引
        assert "idx_ledger_repair_run_step" in names
        assert "idx_ledger_repair_status_created" in names
        # 幂等
        second = await ledger.ensure_indexes()
        assert second == created


class TestLedgerEntryModel:
    def test_lease_expired_property(self):
        entry = StepLedgerEntry(
            run_id="r", plan_id="p", step_id="s", worker="crawl",
            lease_expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        assert entry.lease_expired is True

    def test_lease_not_expired(self):
        entry = StepLedgerEntry(
            run_id="r", plan_id="p", step_id="s", worker="crawl",
            lease_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        assert entry.lease_expired is False

    def test_no_lease_not_expired(self):
        entry = StepLedgerEntry(run_id="r", plan_id="p", step_id="s", worker="crawl")
        assert entry.lease_expired is False
