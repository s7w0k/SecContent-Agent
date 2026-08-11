"""AgentEventStore 单元测试 -- 阶段1 WBS 1.6（统一 EventEnvelope）。"""

from __future__ import annotations

import pytest
from agent.agent_event_store import COLLECTION, AgentEventStore


class FakeCursor:
    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        return []


class FakeCollection:
    def __init__(self) -> None:
        self.inserted: list[dict] = []

    async def insert_one(self, doc: dict):
        self.inserted.append(doc)

    def find(self, *args, **kwargs):
        return FakeCursor()


class FakeDB(dict):
    pass


def _make_store(collection: FakeCollection | None = None) -> tuple[AgentEventStore, FakeCollection]:
    coll = collection or FakeCollection()
    db = FakeDB()
    db[COLLECTION] = coll
    store = AgentEventStore(db, collection=COLLECTION)
    return store, coll


class TestEmit:
    @pytest.mark.asyncio
    async def test_emit_writes_envelope(self):
        store, coll = _make_store()
        env = await store.emit(
            run_id="r1",
            event_type="tool_finished",
            trace_id="t1",
            tool_name="search_knowledge",
            input_hash="h_in",
            result_hash="h_out",
            duration_ms=12,
        )
        assert env is not None
        assert len(coll.inserted) == 1
        doc = coll.inserted[0]
        assert doc["event_id"] == env.event_id
        assert doc["run_id"] == "r1"
        assert doc["schema_version"] == "1.0"
        assert doc["tool_name"] == "search_knowledge"
        assert doc["input_hash"] == "h_in"
        assert doc["result_hash"] == "h_out"
        assert doc["duration_ms"] == 12
        assert doc["sequence"] == 1

    @pytest.mark.asyncio
    async def test_emit_unknown_type_degrades(self):
        store, coll = _make_store()
        env = await store.emit(run_id="r1", event_type="some_unknown_type")
        assert env is not None
        assert env.event_type == "loop_event"
        assert coll.inserted[0]["event_type"] == "loop_event"

    @pytest.mark.asyncio
    async def test_emit_never_raises(self):
        class BrokenCollection:
            async def insert_one(self, doc):
                raise RuntimeError("mongo down")

        db = FakeDB()
        db[COLLECTION] = BrokenCollection()
        store = AgentEventStore(db, collection=COLLECTION)
        env = await store.emit(run_id="r1", event_type="loop_started")
        assert env is None  # 失败仅记日志，不抛出


class TestSequence:
    @pytest.mark.asyncio
    async def test_next_sequence_monotonic(self):
        store, _ = _make_store()
        assert store.next_sequence("r1") == 1
        assert store.next_sequence("r1") == 2
        assert store.next_sequence("r2") == 1

    @pytest.mark.asyncio
    async def test_explicit_sequence(self):
        store, coll = _make_store()
        await store.emit(run_id="r1", event_type="loop_started", sequence=99)
        assert coll.inserted[0]["sequence"] == 99


class TestListAndIndexes:
    @pytest.mark.asyncio
    async def test_list_run_events(self):
        store, _ = _make_store()
        docs = await store.list_run_events("r1")
        assert isinstance(docs, list)

    def test_index_specs(self):
        store, _ = _make_store()
        specs = store.index_specs()
        assert COLLECTION in specs
        assert len(specs[COLLECTION]) == 4
