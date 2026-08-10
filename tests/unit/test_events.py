"""事件系统单元测试 — 阶段三 Step 9。

覆盖：14 种事件类型、PipelineEvent 默认 TTL、EventEmitter 写入/序列/失败静默/
索引、Orchestrator 与 Planner 的事件发射。
"""

from __future__ import annotations

import asyncio
from datetime import UTC

import pytest
from agent.events import COLLECTION, EventEmitter, PipelineEvent
from agent.orchestrator import Orchestrator
from agent.plan_contracts import PipelinePlan, PlanStep
from agent.planner import Planner, PlannerArticleInput, PlannerChoice
from agent.worker_registry import WorkerAdapter, WorkerRegistry, WorkerResult, WorkerSpec

# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _step(
    step_id: str,
    worker: str,
    depends_on: list[str] | None = None,
    policy: str = "required",
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        worker=worker,  # type: ignore[arg-type]
        depends_on=depends_on or [],
        input_refs={},
        policy=policy,  # type: ignore[arg-type]
        timeout_s=30,
        max_attempts=3,
    )


def _plan(steps: list[PlanStep]) -> PipelinePlan:
    return PipelinePlan(
        plan_id="plan-1",
        run_id="run-1",
        planner_version="test-v1",
        input_snapshot_hash="h" * 64,
        steps=steps,
    )


class _FakeCol:
    def __init__(self):
        self.docs: list[dict] = []
        self.created_indexes: list = []

    async def insert_one(self, doc: dict):
        self.docs.append(doc)

    def find(self, *args, **kwargs):
        return self

    async def to_list(self, length: int):
        return self.docs[:length]

    def sort(self, *args, **kwargs):
        return self

    def limit(self, n: int):
        return self

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


class _RecordingEmitter:
    """记录所有发射事件的假 emitter（等价 EventEmitter 接口）。"""

    def __init__(self):
        self.events: list[dict] = []

    async def emit(self, **kwargs):
        self.events.append(kwargs)


class _FakeAdapter(WorkerAdapter):
    def __init__(self, name: str, **spec_kwargs):
        self.name = name  # type: ignore[assignment]
        self.spec = WorkerSpec(name=name, **spec_kwargs)  # type: ignore[arg-type]

    async def execute(self, state, ctx, lease=None) -> WorkerResult:
        return WorkerResult(
            step_id=ctx["step_id"],
            worker=ctx["worker"],
            idempotency_key="k",
            input_hash="i",
            result_hash="r",
            status="succeeded",
            attempt=ctx["attempt"],
        )


def _registry() -> WorkerRegistry:
    registry = WorkerRegistry()
    for name in ("crawl", "filter"):
        registry.register(_FakeAdapter(name, version="v1", concurrency_group="local"))
    return registry


# ═══════════════════════════════════════════════════════════════
# EventType / PipelineEvent / EventEmitter
# ═══════════════════════════════════════════════════════════════


class TestEventModels:
    def test_event_type_has_all_14_types(self):
        from agent.events import EventType

        expected = {
            "plan_requested",
            "plan_created",
            "plan_rejected",
            "plan_fallback",
            "step_scheduled",
            "worker_started",
            "retrying",
            "succeeded",
            "failed",
            "step_skipped",
            "dead_lettered",
            "replayed",
            "run_finished",
            "run_canceled",
        }
        assert set(EventType.__args__) == expected

    def test_pipeline_event_default_expires_90_days(self):
        event = PipelineEvent(event_type="run_finished", run_id="run-1")
        assert event.created_at.tzinfo is UTC
        span = (event.expires_at - event.created_at).total_seconds()
        assert 89 * 86400 <= span <= 90 * 86400 + 1

    def test_pipeline_event_defaults(self):
        event = PipelineEvent(event_type="succeeded", run_id="run-1", step_id="s1", worker="crawl")
        assert event.attempt == 0
        assert event.sequence == 0
        assert event.duration_ms == 0
        assert event.error_type is None
        assert event.status == ""

    def test_pipeline_event_rejects_unknown_type(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PipelineEvent(event_type="nope", run_id="run-1")


class TestEventEmitter:
    @pytest.mark.asyncio
    async def test_emit_persists_and_sequence_increments(self):
        db = _FakeDB()
        emitter = EventEmitter(db)
        await emitter.emit(event_type="plan_requested", run_id="run-1")
        await emitter.emit(event_type="succeeded", run_id="run-1", step_id="s1", attempt=1)
        await emitter.emit(event_type="succeeded", run_id="run-1", step_id="s2", attempt=1)

        docs = db[COLLECTION].docs
        assert [d["sequence"] for d in docs] == [1, 2, 3]
        assert [d["event_type"] for d in docs] == ["plan_requested", "succeeded", "succeeded"]
        assert docs[1]["run_id"] == "run-1"
        assert docs[1]["step_id"] == "s1"

    @pytest.mark.asyncio
    async def test_emit_failure_is_silent(self):
        class BrokenCol:
            async def insert_one(self, doc: dict):
                raise RuntimeError("db down")

        class BrokenDB(dict):
            def __getitem__(self, name):
                return BrokenCol()

        emitter = EventEmitter(BrokenDB())
        # 绝不抛出
        result = await emitter.emit(event_type="succeeded", run_id="run-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_run_events_orders_by_insertion(self):
        db = _FakeDB()
        emitter = EventEmitter(db)
        await emitter.emit(event_type="plan_requested", run_id="run-1")
        await emitter.emit(event_type="run_finished", run_id="run-1")

        events = await emitter.list_run_events("run-1")
        assert [e.event_type for e in events] == ["plan_requested", "run_finished"]
        assert isinstance(events[0], PipelineEvent)

    def test_index_specs_three_indexes(self):
        db = _FakeDB()
        specs = EventEmitter(db).index_specs()
        names = {index.document["name"] for index in specs[COLLECTION]}
        assert names == {
            "idx_pipeline_events_run_created",
            "idx_pipeline_events_type_created",
            "ttl_pipeline_events_expires",
        }

    @pytest.mark.asyncio
    async def test_ensure_indexes(self):
        db = _FakeDB()
        emitter = EventEmitter(db)
        names = await emitter.ensure_indexes()
        assert len(names) == 3
        assert db[COLLECTION].created_indexes


# ═══════════════════════════════════════════════════════════════
# Orchestrator 事件发射
# ═══════════════════════════════════════════════════════════════


class TestOrchestratorEvents:
    @pytest.mark.asyncio
    async def test_successful_run_emits_expected_sequence(self):
        registry = _registry()
        emitter = _RecordingEmitter()
        orchestrator = Orchestrator(registry, emitter=emitter)
        plan = _plan([_step("s1", "crawl"), _step("s2", "filter", ["s1"])])

        outcome = await orchestrator.run(plan)

        assert outcome.status == "completed"
        types = [e["event_type"] for e in emitter.events]
        # 波 1: s1 scheduled → worker_started → succeeded；波 2: s2 同理；最后 run_finished
        assert types == [
            "step_scheduled",
            "worker_started",
            "succeeded",
            "step_scheduled",
            "worker_started",
            "succeeded",
            "run_finished",
        ]
        assert emitter.events[0]["run_id"] == "run-1"
        assert emitter.events[0]["plan_id"] == "plan-1"
        assert emitter.events[0]["step_id"] == "s1"
        assert emitter.events[0]["worker"] == "crawl"
        assert emitter.events[2]["status"] == "succeeded"
        assert emitter.events[-1]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_canceled_run_emits_run_canceled(self):
        registry = _registry()
        emitter = _RecordingEmitter()
        orchestrator = Orchestrator(registry, emitter=emitter)
        plan = _plan([_step("s1", "crawl")])
        cancel = asyncio.Event()
        cancel.set()

        outcome = await orchestrator.run(plan, cancel_event=cancel)

        assert outcome.status == "canceled"
        assert emitter.events[-1]["event_type"] == "run_canceled"

    @pytest.mark.asyncio
    async def test_no_emitter_is_safe(self):
        registry = _registry()
        orchestrator = Orchestrator(registry)
        plan = _plan([_step("s1", "crawl")])

        outcome = await orchestrator.run(plan)

        assert outcome.status == "completed"


# ═══════════════════════════════════════════════════════════════
# Planner 事件发射
# ═══════════════════════════════════════════════════════════════


class TestPlannerEvents:
    def _articles(self) -> list[PlannerArticleInput]:
        return [PlannerArticleInput(id="art-1", title="t", summary="s", status="crawled")]

    @pytest.mark.asyncio
    async def test_disabled_planner_emits_requested_and_fallback(self):
        emitter = _RecordingEmitter()
        planner = Planner(enabled=False, emitter=emitter)
        outcome = await planner.plan(run_id="run-1", articles=self._articles())

        assert outcome.source == "fallback"
        types = [e["event_type"] for e in emitter.events]
        assert types == ["plan_requested", "plan_fallback"]
        assert emitter.events[0]["run_id"] == "run-1"
        assert emitter.events[1]["run_id"] == "run-1"
        assert emitter.events[1]["status"] == "fallback"

    @pytest.mark.asyncio
    async def test_llm_planner_emits_requested_and_created(self):
        class FakeWrapper:
            async def invoke_structured(self, **kwargs):
                return PlannerChoice(
                    needs_fulltext=True,
                    breaking_article_ids=[],
                    article_ids=["art-1"],
                    product_ids=[],
                    score_threshold=80,
                )

        emitter = _RecordingEmitter()
        planner = Planner(
            llm_wrapper=FakeWrapper(),
            enabled=True,
            planner_model="fake-model",
            emitter=emitter,
        )
        outcome = await planner.plan(run_id="run-1", articles=self._articles())

        assert outcome.source == "planner"
        types = [e["event_type"] for e in emitter.events]
        assert types == ["plan_requested", "plan_created"]
        assert emitter.events[1]["plan_id"] == outcome.plan.plan_id
        assert emitter.events[1]["status"] == "accepted"
        assert emitter.events[1]["result_hash"] == outcome.plan.plan_hash
