"""Persistent multi-turn task state with tenant-scoped optimistic locking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from agent.contracts.task import ConversationTurn, SlotState, TaskEnvelope
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import DuplicateKeyError

COLLECTION = "conversation_tasks"
SCHEMA_VERSION = "1.0"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    PENDING = "pending"
    WAITING_USER = "waiting_user"
    WAITING_APPROVAL = "waiting_approval"
    PLANNING = "planning"
    RUNNING = "running"
    RETRYING = "retrying"
    DEGRADED = "degraded"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    STOPPED = "stopped"


ACTIVE_TASK_STATUSES = frozenset(
    {
        TaskStatus.PENDING,
        TaskStatus.WAITING_USER,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.RETRYING,
        TaskStatus.DEGRADED,
    }
)


class TaskStateError(Exception):
    """Base persistent task state error."""


class TaskStateConflictError(TaskStateError):
    """The caller attempted to overwrite a newer task version."""


class TaskStateNotFoundError(TaskStateError):
    """No task exists inside the supplied tenant and user scope."""


class TaskCheckpoint(BaseModel):
    """Reference to a recoverable RuntimeState checkpoint."""

    run_id: str = Field(..., min_length=1, max_length=100)
    runtime_checkpoint_version: int = Field(..., ge=1)
    plan_version: int = Field(default=0, ge=0)
    status: str = Field(default="", max_length=32)
    artifact_refs: list[str] = Field(default_factory=list, max_length=100)
    pending_questions: list[str] = Field(default_factory=list, max_length=10)
    saved_at: datetime = Field(default_factory=_utc_now)


class TaskState(BaseModel):
    """Conversation task state persisted independently of the runtime process."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = SCHEMA_VERSION
    task_id: str = Field(..., min_length=1, max_length=100)
    thread_id: str = Field(..., min_length=1, max_length=100)
    user_id: str = Field(..., min_length=1, max_length=100)
    tenant_id: str = Field(..., min_length=1, max_length=100)
    envelope: TaskEnvelope
    slot_states: dict[str, SlotState] = Field(default_factory=dict)
    turns: list[ConversationTurn] = Field(default_factory=list, max_length=500)
    status: TaskStatus = TaskStatus.PENDING
    current_run_id: str = Field(default="", max_length=100)
    checkpoint: TaskCheckpoint | None = None
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime = Field(default_factory=lambda: _utc_now() + timedelta(days=30))

    @model_validator(mode="after")
    def validate_scope_and_slots(self) -> TaskState:
        expected = (self.task_id, self.thread_id, self.user_id, self.tenant_id)
        actual = (
            self.envelope.task_id,
            self.envelope.thread_id,
            self.envelope.user_id,
            self.envelope.tenant_id,
        )
        if expected != actual:
            raise ValueError("task state identity must match TaskEnvelope identity")
        if not self.slot_states:
            self.slot_states = self.envelope.slot_states()
        unknown = set(self.slot_states) - set(TaskEnvelope.SLOT_NAMES)
        if unknown:
            raise ValueError(f"unknown task slots: {', '.join(sorted(unknown))}")
        sequences = [turn.sequence for turn in self.turns]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("conversation turn sequences must be contiguous from 1")
        if len({turn.turn_id for turn in self.turns}) != len(self.turns):
            raise ValueError("conversation turn_id must be unique within a task")
        return self

    @classmethod
    def create(cls, envelope: TaskEnvelope, *, ttl_days: int = 30) -> TaskState:
        if not envelope.tenant_id:
            raise ValueError("tenant_id is required for persistent task state")
        stamp = _utc_now()
        return cls(
            task_id=envelope.task_id,
            thread_id=envelope.thread_id,
            user_id=envelope.user_id,
            tenant_id=envelope.tenant_id,
            envelope=envelope,
            slot_states=envelope.slot_states(),
            created_at=stamp,
            updated_at=stamp,
            expires_at=stamp + timedelta(days=max(1, ttl_days)),
        )


class TaskStateStoreProtocol(Protocol):
    async def create(self, state: TaskState) -> TaskState: ...

    async def get(self, task_id: str, *, user_id: str, tenant_id: str) -> TaskState | None: ...

    async def compare_and_set(self, state: TaskState, *, expected_version: int) -> TaskState: ...

    async def append_turn(
        self,
        task_id: str,
        turn: ConversationTurn,
        *,
        user_id: str,
        tenant_id: str,
        expected_version: int,
    ) -> TaskState: ...


class TaskStateStore:
    """MongoDB adapter; Agent tools never receive the underlying collection."""

    def __init__(self, db: Any, *, collection: str = COLLECTION):
        self.collection_name = collection
        self.col = db[collection]

    def index_specs(self) -> dict[str, list[IndexModel]]:
        return {
            self.collection_name: [
                IndexModel([("task_id", ASCENDING)], unique=True, name="uq_task_state_task_id"),
                IndexModel(
                    [
                        ("tenant_id", ASCENDING),
                        ("user_id", ASCENDING),
                        ("thread_id", ASCENDING),
                        ("updated_at", DESCENDING),
                    ],
                    name="idx_task_state_scope_thread_updated",
                ),
                IndexModel(
                    [
                        ("tenant_id", ASCENDING),
                        ("user_id", ASCENDING),
                        ("status", ASCENDING),
                        ("updated_at", DESCENDING),
                    ],
                    name="idx_task_state_scope_active",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="ttl_task_state_expires",
                ),
            ]
        }

    async def ensure_indexes(self) -> list[str]:
        return await self.col.create_indexes(self.index_specs()[self.collection_name])

    async def create(self, state: TaskState) -> TaskState:
        try:
            await self.col.insert_one(state.model_dump(mode="python"))
            return state
        except DuplicateKeyError as exc:
            existing = await self.get(
                state.task_id, user_id=state.user_id, tenant_id=state.tenant_id
            )
            if (
                existing is not None
                and existing.envelope.fingerprint() == state.envelope.fingerprint()
            ):
                return existing
            raise TaskStateConflictError(f"task already exists: {state.task_id}") from exc

    async def get(self, task_id: str, *, user_id: str, tenant_id: str) -> TaskState | None:
        if not user_id or not tenant_id:
            raise ValueError("user_id and tenant_id are required")
        doc = await self.col.find_one(
            {"task_id": task_id, "user_id": user_id, "tenant_id": tenant_id}
        )
        return TaskState.model_validate(doc) if doc is not None else None

    async def compare_and_set(self, state: TaskState, *, expected_version: int) -> TaskState:
        if state.version != expected_version:
            raise TaskStateConflictError(
                f"task conflict: expected version={expected_version}, state version={state.version}"
            )
        updated = state.model_copy(
            update={"version": expected_version + 1, "updated_at": _utc_now()}
        )
        result = await self.col.replace_one(
            {
                "task_id": state.task_id,
                "user_id": state.user_id,
                "tenant_id": state.tenant_id,
                "version": expected_version,
            },
            updated.model_dump(mode="python"),
            upsert=False,
        )
        if result.matched_count == 0:
            raise TaskStateConflictError(f"task conflict: expected version={expected_version}")
        return updated

    async def append_turn(
        self,
        task_id: str,
        turn: ConversationTurn,
        *,
        user_id: str,
        tenant_id: str,
        expected_version: int,
    ) -> TaskState:
        state = await self.get(task_id, user_id=user_id, tenant_id=tenant_id)
        if state is None:
            raise TaskStateNotFoundError("task not found")
        existing = next((item for item in state.turns if item.turn_id == turn.turn_id), None)
        if existing is not None:
            if existing.content_hash != turn.content_hash:
                raise TaskStateConflictError("turn_id already exists with different content")
            return state
        if turn.sequence != len(state.turns) + 1:
            raise TaskStateConflictError("turn sequence is out of order")
        candidate = state.model_copy(update={"turns": [*state.turns, turn]})
        return await self.compare_and_set(candidate, expected_version=expected_version)

    async def checkpoint(
        self,
        task_id: str,
        checkpoint: TaskCheckpoint,
        *,
        user_id: str,
        tenant_id: str,
        expected_version: int,
        status: TaskStatus | None = None,
    ) -> TaskState:
        state = await self.get(task_id, user_id=user_id, tenant_id=tenant_id)
        if state is None:
            raise TaskStateNotFoundError("task not found")
        candidate = state.model_copy(
            update={
                "checkpoint": checkpoint,
                "current_run_id": checkpoint.run_id,
                "status": status or state.status,
            }
        )
        return await self.compare_and_set(candidate, expected_version=expected_version)

    async def list_active(
        self, *, user_id: str, tenant_id: str, limit: int = 50
    ) -> list[TaskState]:
        if not user_id or not tenant_id:
            raise ValueError("user_id and tenant_id are required")
        cursor = (
            self.col.find(
                {
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "status": {"$in": [status.value for status in ACTIVE_TASK_STATUSES]},
                }
            )
            .sort("updated_at", -1)
            .limit(max(1, min(limit, 200)))
        )
        docs = await cursor.to_list(length=max(1, min(limit, 200)))
        return [TaskState.model_validate(doc) for doc in docs]


class InMemoryTaskStateStore:
    """Deterministic fake store implementing the same scope and CAS semantics."""

    def __init__(self):
        self._states: dict[str, TaskState] = {}

    async def create(self, state: TaskState) -> TaskState:
        existing = self._states.get(state.task_id)
        if existing is not None:
            if (
                existing.user_id == state.user_id
                and existing.tenant_id == state.tenant_id
                and existing.envelope.fingerprint() == state.envelope.fingerprint()
            ):
                return existing.model_copy(deep=True)
            raise TaskStateConflictError(f"task already exists: {state.task_id}")
        self._states[state.task_id] = state.model_copy(deep=True)
        return state.model_copy(deep=True)

    async def get(self, task_id: str, *, user_id: str, tenant_id: str) -> TaskState | None:
        if not user_id or not tenant_id:
            raise ValueError("user_id and tenant_id are required")
        state = self._states.get(task_id)
        if state is None or state.user_id != user_id or state.tenant_id != tenant_id:
            return None
        return state.model_copy(deep=True)

    async def compare_and_set(self, state: TaskState, *, expected_version: int) -> TaskState:
        current = self._states.get(state.task_id)
        if (
            current is None
            or current.user_id != state.user_id
            or current.tenant_id != state.tenant_id
            or current.version != expected_version
            or state.version != expected_version
        ):
            raise TaskStateConflictError(f"task conflict: expected version={expected_version}")
        updated = state.model_copy(
            update={"version": expected_version + 1, "updated_at": _utc_now()}, deep=True
        )
        self._states[state.task_id] = updated
        return updated.model_copy(deep=True)

    async def append_turn(
        self,
        task_id: str,
        turn: ConversationTurn,
        *,
        user_id: str,
        tenant_id: str,
        expected_version: int,
    ) -> TaskState:
        state = await self.get(task_id, user_id=user_id, tenant_id=tenant_id)
        if state is None:
            raise TaskStateNotFoundError("task not found")
        existing = next((item for item in state.turns if item.turn_id == turn.turn_id), None)
        if existing is not None:
            if existing.content_hash != turn.content_hash:
                raise TaskStateConflictError("turn_id already exists with different content")
            return state
        if turn.sequence != len(state.turns) + 1:
            raise TaskStateConflictError("turn sequence is out of order")
        return await self.compare_and_set(
            state.model_copy(update={"turns": [*state.turns, turn]}),
            expected_version=expected_version,
        )

    async def checkpoint(
        self,
        task_id: str,
        checkpoint: TaskCheckpoint,
        *,
        user_id: str,
        tenant_id: str,
        expected_version: int,
        status: TaskStatus | None = None,
    ) -> TaskState:
        state = await self.get(task_id, user_id=user_id, tenant_id=tenant_id)
        if state is None:
            raise TaskStateNotFoundError("task not found")
        candidate = state.model_copy(
            update={
                "checkpoint": checkpoint,
                "current_run_id": checkpoint.run_id,
                "status": status or state.status,
            }
        )
        return await self.compare_and_set(candidate, expected_version=expected_version)

    async def list_active(
        self, *, user_id: str, tenant_id: str, limit: int = 50
    ) -> list[TaskState]:
        matched = [
            state.model_copy(deep=True)
            for state in self._states.values()
            if state.user_id == user_id
            and state.tenant_id == tenant_id
            and state.status in ACTIVE_TASK_STATUSES
        ]
        return sorted(matched, key=lambda state: state.updated_at, reverse=True)[:limit]
