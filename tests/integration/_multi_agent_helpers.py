"""MultiAgent 集成测试共享辅助 — 阶段三 Step 10。

供 test_planned_pipeline / test_pipeline_recovery /
test_multi_agent_fault_injection 复用：
  - Fake Mongo（Motor 风格异步接口，与单元测试同构）；
  - 可配置行为的 FakeAdapter（成功/失败/重试/超时/挂起/业务写回调）；
  - Fake LLM（返回固定 PlannerChoice 或抛错）；
  - 运行时组装（validator/ledger/orchestrator/planner）与默认计划构造。
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from pymongo import ReturnDocument

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.execution_step_ledger import ExecutionStepLedger
from agent.orchestrator import Orchestrator
from agent.plan_contracts import (
    PipelinePlan,
    PlanValidator,
    _step,
    build_default_plan,
    input_snapshot_hash,
)
from agent.planner import PLANNER_VERSION, Planner, PlannerChoice
from agent.worker_registry import (
    WorkerAdapter,
    WorkerRegistry,
    WorkerResult,
    WorkerSpec,
)

DEFAULT_WORKERS = ["crawl", "classify", "filter", "score", "draft", "quality_check", "rewrite", "review"]


# ═══════════════════════════════════════════════════════════════
# Fake Mongo
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


def make_db() -> _FakeDB:
    return _FakeDB()


# ═══════════════════════════════════════════════════════════════
# Fake Worker
# ═══════════════════════════════════════════════════════════════


class _FakeAdapter(WorkerAdapter):
    """可配置行为的测试 Worker（成功/失败/重试 N 次/超时/挂起/业务写回调）。"""

    def __init__(
        self,
        name: str,
        *,
        status: str = "succeeded",
        retryable: bool = False,
        error_type: str = "boom",
        attempts_until_success: int = 1,
        delay: float = 0.0,
        hang: bool = False,
        timeout_s: int = 60,
        max_attempts: int = 3,
        concurrency_group: str = "local",
        on_execute: Callable[[dict, dict, Any], Any] | None = None,
    ):
        self.name = name  # type: ignore[assignment]
        self.version = "test-v1"
        self.executions: list[dict[str, Any]] = []
        self.spec = WorkerSpec(
            name=name,
            version="test-v1",
            timeout_s=timeout_s,
            max_attempts=max_attempts,
            concurrency_group=concurrency_group,
        )
        self._status = status
        self._retryable = retryable
        self._error_type = error_type
        self._attempts_until_success = attempts_until_success
        self._delay = delay
        self._hang = hang
        self._on_execute = on_execute

    async def execute(self, state: dict, ctx: dict, lease=None) -> WorkerResult:
        self.executions.append({"ctx": dict(ctx), "lease": lease})
        if self._on_execute is not None:
            result = self._on_execute(state, ctx, lease)
            if asyncio.iscoroutine(result):
                await result
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._hang:
            await asyncio.Event().wait()
        attempt = int(ctx.get("attempt", 1))
        if attempt < self._attempts_until_success:
            return WorkerResult(
                step_id=ctx["step_id"], worker=self.name, status="failed",
                error_type=self._error_type, error_message="retry me",
                retryable=True, attempt=attempt, duration_ms=1,
            )
        if self._status != "succeeded":
            return WorkerResult(
                step_id=ctx["step_id"], worker=self.name, status="failed",
                error_type=self._error_type, error_message="boom",
                retryable=self._retryable, attempt=attempt, duration_ms=1,
            )
        resolved = self.resolve_input(state, ctx)
        input_hash = self.compute_input_hash(resolved)
        return WorkerResult(
            step_id=ctx["step_id"], worker=self.name,
            idempotency_key=self.idempotency_key(ctx, input_hash),
            input_hash=input_hash, result_hash="sha256:ok",
            status="succeeded", attempt=attempt, duration_ms=1,
            output={"current_phase": ctx["step_id"]},
        )


def make_registry(
    workers: list[str] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> WorkerRegistry:
    """默认注册 7 个必须 Worker；overrides 按 name 配置行为。"""
    registry = WorkerRegistry()
    for name in workers or DEFAULT_WORKERS:
        cfg = dict(overrides or {}).get(name, {})
        registry.register(_FakeAdapter(name, **cfg))
    return registry


def adapter_by_name(registry: WorkerRegistry, name: str) -> _FakeAdapter:
    adapter = registry.get(name)
    assert adapter is not None and isinstance(adapter, _FakeAdapter)
    return adapter


# ═══════════════════════════════════════════════════════════════
# Fake LLM / Planner
# ═══════════════════════════════════════════════════════════════


class _FakeLLMWrapper:
    """返回固定 PlannerChoice 或抛错。"""

    def __init__(self, choice: PlannerChoice | None = None, error: Exception | None = None):
        self.choice = choice
        self.error = error
        self.calls = 0

    async def invoke_structured(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.choice if self.choice is not None else PlannerChoice()


def make_planner(db=None, wrapper=None, *, enabled: bool = True, model: str = "test:planner") -> Planner:
    return Planner(
        llm_wrapper=wrapper,
        db=db,
        enabled=enabled,
        planner_model=model,
        validator=PlanValidator(),
        planner_version=PLANNER_VERSION,
    )


# ═══════════════════════════════════════════════════════════════
# 运行时组装
# ═══════════════════════════════════════════════════════════════


def make_execution_stack(db, registry, *, owner_id: str = "orch", lease_seconds: int = 120, max_attempts: int = 3):
    """组装 validator/ledger/orchestrator（不含 planner）。"""
    validator = PlanValidator()
    ledger = ExecutionStepLedger(db, lease_seconds=lease_seconds)
    orchestrator = Orchestrator(
        registry,
        owner_id=owner_id,
        max_concurrency=5,
        user_concurrency=2,
        worker_concurrency=2,
        lease_seconds=lease_seconds,
        default_max_attempts=max_attempts,
        ledger=ledger,
    )
    return validator, ledger, orchestrator


# ═══════════════════════════════════════════════════════════════
# 计划构造
# ═══════════════════════════════════════════════════════════════


def default_plan(run_id: str = "run-1", *, article_ids: list[str] | None = None, needs_fulltext: bool = False) -> PipelinePlan:
    """确定性默认计划（无全文 → 7 步：crawl/classify/filter/score/draft/quality_check/review）。"""
    articles = article_ids or ["a1"]
    snapshot = input_snapshot_hash(user_id="u1", article_ids=articles)
    return build_default_plan(
        run_id=run_id,
        input_snapshot_hash_value=snapshot,
        planner_version=PLANNER_VERSION,
        user_id="u1",
        product_ids=["p1"],
        article_ids=articles,
        needs_fulltext=needs_fulltext,
    )


def two_wave_plan() -> PipelinePlan:
    """两波依赖计划（用于取消/竞态注入）。"""
    steps = [
        _step("s1_a", "crawl", [], {}, timeout_s=60),
        _step("s2_b", "classify", ["s1_a"], {}, timeout_s=60),
    ]
    return PipelinePlan(
        plan_id="plan-2w",
        run_id="run-2w",
        planner_version="test-v1",
        input_snapshot_hash="sha256:h",
        steps=steps,
    )


def fixed_now() -> datetime:
    return datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
