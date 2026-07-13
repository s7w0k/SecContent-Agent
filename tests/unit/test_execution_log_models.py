"""L1 执行日志模型、索引、序号和状态机测试。"""

from datetime import UTC, datetime, timedelta

import pytest
from db.mongo import MongoDB
from execution_logs.catalog import Action, ExecutionType, Phase, Relation, Scope
from models.execution_log import (
    EventType,
    ExecutionEvent,
    ExecutionLink,
    ExecutionRun,
    LogService,
    RunStatus,
    validate_run_status_transition,
)
from pydantic import ValidationError


def test_execution_run_enforces_scope_identity() -> None:
    run = ExecutionRun(
        trace_id="trace-1",
        scope=Scope.USER,
        execution_type=ExecutionType.DRAFT,
        owner_user_id="user-a",
    )
    assert run.status == RunStatus.PENDING
    assert run.next_sequence == 0

    with pytest.raises(ValidationError, match="owner_user_id"):
        ExecutionRun(
            trace_id="trace-2",
            scope=Scope.USER,
            execution_type=ExecutionType.DRAFT,
        )
    with pytest.raises(ValidationError, match="initiator_user_id"):
        ExecutionRun(
            trace_id="trace-3",
            scope=Scope.SHARED,
            execution_type=ExecutionType.CRAWL_OVERSEAS,
        )


def test_execution_event_enforces_action_phase_and_scope() -> None:
    event = ExecutionEvent(
        execution_id="exec-1",
        trace_id="trace-1",
        sequence=1,
        scope=Scope.SHARED,
        initiator_user_id="user-a",
        service=LogService.MCP_CRAWL,
        component="crawler",
        phase=Phase.CRAWL,
        action=Action.SITE_FEED_RESULT,
        event_type=EventType.SUCCESS,
        message="站点抓取完成",
    )
    assert event.action == Action.SITE_FEED_RESULT

    with pytest.raises(ValidationError, match="must use phase crawl"):
        ExecutionEvent(
            execution_id="exec-1",
            trace_id="trace-1",
            sequence=2,
            scope=Scope.SHARED,
            initiator_user_id="user-a",
            service=LogService.BACKEND,
            component="pipeline",
            phase=Phase.TASK,
            action=Action.SITE_FEED_RESULT,
            event_type=EventType.INFO,
            message="错误阶段",
        )


def test_execution_link_uses_frozen_relation_enum() -> None:
    link = ExecutionLink(
        user_id="user-b",
        task_id="task-1",
        user_execution_id="user-exec-1",
        shared_execution_id="shared-exec-1",
        relation=Relation.REUSER,
    )
    assert link.relation == Relation.REUSER


def test_run_status_machine_and_terminal_metadata() -> None:
    started = datetime.now(UTC)
    run = ExecutionRun(
        trace_id="trace-1",
        scope=Scope.USER,
        execution_type=ExecutionType.PIPELINE_V2,
        owner_user_id="user-a",
        started_at=started,
    )
    run.transition_to(RunStatus.RUNNING)
    run.transition_to(RunStatus.COMPLETED, at=started + timedelta(seconds=2))
    assert run.completed_at == started + timedelta(seconds=2)
    assert run.duration_ms == 2000
    with pytest.raises(ValueError, match="completed -> running"):
        run.transition_to(RunStatus.RUNNING)
    assert validate_run_status_transition(RunStatus.RUNNING, RunStatus.RUNNING) == RunStatus.RUNNING


@pytest.mark.asyncio
async def test_allocate_execution_sequence_is_atomic_and_rejects_missing_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCollection:
        def __init__(self) -> None:
            self.sequence = 0

        async def find_one_and_update(self, query, update, **kwargs):
            assert query == {"execution_id": "exec-1"}
            assert update == {"$inc": {"next_sequence": 1}}
            self.sequence += 1
            return {"next_sequence": self.sequence}

    collection = FakeCollection()
    monkeypatch.setattr(
        MongoDB,
        "get_collection",
        classmethod(lambda cls, name: collection),
    )
    assert await MongoDB.allocate_execution_sequence("exec-1") == 1
    assert await MongoDB.allocate_execution_sequence("exec-1") == 2

    async def missing(*args, **kwargs):
        return None

    collection.find_one_and_update = missing
    with pytest.raises(LookupError, match="exec-missing"):
        await MongoDB.allocate_execution_sequence("exec-missing")


@pytest.mark.asyncio
async def test_execution_log_indexes_include_unique_ordering_and_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list] = {}

    class FakeCollection:
        def __init__(self, name: str) -> None:
            self.name = name

        async def create_indexes(self, indexes):
            captured[self.name] = indexes
            return [index.document["name"] for index in indexes]

        async def drop_index(self, name: str) -> None:
            return None

    collections: dict[str, FakeCollection] = {}

    def get_collection(cls, name: str) -> FakeCollection:
        return collections.setdefault(name, FakeCollection(name))

    monkeypatch.setattr(MongoDB, "get_collection", classmethod(get_collection))
    created = await MongoDB.ensure_indexes()

    assert {"execution_runs", "execution_events", "execution_links"} <= created.keys()
    event_indexes = {
        index.document["name"]: index.document for index in captured["execution_events"]
    }
    assert event_indexes["idx_event_event_id"]["unique"] is True
    assert event_indexes["idx_event_execution_sequence"]["unique"] is True
    assert event_indexes["idx_event_expires"]["expireAfterSeconds"] == 0
    run_indexes = {index.document["name"]: index.document for index in captured["execution_runs"]}
    link_indexes = {index.document["name"]: index.document for index in captured["execution_links"]}
    assert run_indexes["idx_run_execution_id"]["unique"] is True
    assert run_indexes["idx_run_expires"]["expireAfterSeconds"] == 0
    assert link_indexes["idx_link_user_shared_task"]["unique"] is True
    assert link_indexes["idx_link_expires"]["expireAfterSeconds"] == 0


def test_execution_log_settings_defaults_and_bounds() -> None:
    from config import Settings

    settings = Settings(DEEPSEEK_API_KEY="test", _env_file=None)
    assert settings.EXECUTION_LOG_LEVEL == "INFO"
    assert settings.EXECUTION_LOG_RUN_RETENTION_DAYS == 90
    assert settings.EXECUTION_LOG_EVENT_RETENTION_DAYS == 30
    assert settings.EXECUTION_LOG_QUEUE_SIZE == 10000
    assert settings.EXECUTION_LOG_BATCH_SIZE == 50
    with pytest.raises(ValidationError):
        Settings(DEEPSEEK_API_KEY="test", EXECUTION_LOG_QUEUE_SIZE=10, _env_file=None)
