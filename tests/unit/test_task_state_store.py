from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agent.contracts.task import ConversationTurn, TaskEnvelope, TaskIntent
from agent.task_state_store import (
    InMemoryTaskStateStore,
    TaskCheckpoint,
    TaskState,
    TaskStateConflictError,
    TaskStateStore,
    TaskStatus,
)


def _envelope(*, task_id: str = "task-1", user_id: str = "user-1", tenant_id: str = "t-1"):
    return TaskEnvelope.from_user_input(
        task_id=task_id,
        thread_id="thread-1",
        user_id=user_id,
        tenant_id=tenant_id,
        goal="生成稿件",
        intent=TaskIntent.GENERATE_DRAFT,
        acceptance_criteria=["生成初稿"],
    )


def _turn(turn_id: str, sequence: int, content: str = "继续") -> ConversationTurn:
    return ConversationTurn(turn_id=turn_id, sequence=sequence, role="user", content=content)


async def test_in_memory_store_round_trip_and_tenant_isolation():
    store = InMemoryTaskStateStore()
    state = TaskState.create(_envelope())
    await store.create(state)
    loaded = await store.get("task-1", user_id="user-1", tenant_id="t-1")
    assert loaded == state
    assert await store.get("task-1", user_id="user-1", tenant_id="t-2") is None
    assert await store.get("task-1", user_id="other", tenant_id="t-1") is None


async def test_create_is_idempotent_only_for_identical_scoped_task():
    store = InMemoryTaskStateStore()
    state = TaskState.create(_envelope())
    assert await store.create(state) == state
    assert await store.create(state) == state
    with pytest.raises(TaskStateConflictError):
        await store.create(TaskState.create(_envelope(user_id="other")))


async def test_compare_and_set_rejects_stale_writer():
    store = InMemoryTaskStateStore()
    base = await store.create(TaskState.create(_envelope()))
    updated = await store.compare_and_set(
        base.model_copy(update={"status": TaskStatus.RUNNING}), expected_version=1
    )
    assert updated.version == 2
    with pytest.raises(TaskStateConflictError):
        await store.compare_and_set(base, expected_version=1)


async def test_append_turn_is_idempotent_and_rejects_out_of_order():
    store = InMemoryTaskStateStore()
    state = await store.create(TaskState.create(_envelope()))
    state = await store.append_turn(
        state.task_id,
        _turn("turn-1", 1),
        user_id=state.user_id,
        tenant_id=state.tenant_id,
        expected_version=state.version,
    )
    duplicate = await store.append_turn(
        state.task_id,
        _turn("turn-1", 1),
        user_id=state.user_id,
        tenant_id=state.tenant_id,
        expected_version=state.version,
    )
    assert duplicate.version == state.version
    assert len(duplicate.turns) == 1
    with pytest.raises(TaskStateConflictError):
        await store.append_turn(
            state.task_id,
            _turn("turn-3", 3),
            user_id=state.user_id,
            tenant_id=state.tenant_id,
            expected_version=state.version,
        )


async def test_checkpoint_supports_restart_recovery_reference():
    store = InMemoryTaskStateStore()
    state = await store.create(TaskState.create(_envelope()))
    checkpoint = TaskCheckpoint(
        run_id="run-1",
        runtime_checkpoint_version=4,
        plan_version=2,
        status="waiting_user",
        artifact_refs=["draft-1"],
        pending_questions=["是否保存？"],
        saved_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    updated = await store.checkpoint(
        state.task_id,
        checkpoint,
        user_id=state.user_id,
        tenant_id=state.tenant_id,
        expected_version=state.version,
        status=TaskStatus.WAITING_USER,
    )
    restored = await store.get(state.task_id, user_id=state.user_id, tenant_id=state.tenant_id)
    assert restored == updated
    assert restored.checkpoint.runtime_checkpoint_version == 4
    assert restored.current_run_id == "run-1"


async def test_list_active_is_scoped_and_excludes_terminal():
    store = InMemoryTaskStateStore()
    active = await store.create(TaskState.create(_envelope(task_id="task-active")))
    done = await store.create(TaskState.create(_envelope(task_id="task-done")))
    await store.compare_and_set(
        done.model_copy(update={"status": TaskStatus.COMPLETED}), expected_version=done.version
    )
    await store.create(TaskState.create(_envelope(task_id="task-other", tenant_id="t-2")))
    result = await store.list_active(user_id="user-1", tenant_id="t-1")
    assert [item.task_id for item in result] == [active.task_id]


def test_mongo_adapter_defines_scope_cas_and_ttl_indexes():
    class _DB(dict):
        def __getitem__(self, key):
            return self.setdefault(key, object())

    store = TaskStateStore(_DB())
    specs = store.index_specs()[store.collection_name]
    names = {spec.document["name"] for spec in specs}
    assert names == {
        "uq_task_state_task_id",
        "idx_task_state_scope_thread_updated",
        "idx_task_state_scope_active",
        "ttl_task_state_expires",
    }
