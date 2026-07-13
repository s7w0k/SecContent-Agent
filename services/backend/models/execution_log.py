"""多租户全链路执行日志 MongoDB 数据模型。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from execution_logs.catalog import Action, ErrorCode, ExecutionType, Phase, Relation, Scope
from pydantic import BaseModel, Field, model_validator


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid4())


def _run_expires_at() -> datetime:
    return _utc_now() + timedelta(days=90)


def _event_expires_at() -> datetime:
    return _utc_now() + timedelta(days=30)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    START = "start"
    PROGRESS = "progress"
    RETRY = "retry"
    SUCCESS = "success"
    FAILURE = "failure"
    INFO = "info"


class EventLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    SUCCESS = "SUCCESS"


class LogService(StrEnum):
    BACKEND = "backend"
    MCP_CRAWL = "mcp-crawl"
    MCP_WEWE = "mcp-wewe"
    MONGODB = "mongodb"


RUN_STATUS_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def validate_run_status_transition(current: RunStatus | str, target: RunStatus | str) -> RunStatus:
    """校验并返回目标状态；同状态更新允许幂等执行。"""

    current_status = RunStatus(current)
    target_status = RunStatus(target)
    if target_status == current_status:
        return target_status
    if target_status not in RUN_STATUS_TRANSITIONS[current_status]:
        raise ValueError(
            f"invalid execution run status transition: {current_status.value} -> "
            f"{target_status.value}"
        )
    return target_status


class ExecutionRun(BaseModel):
    """一次用户、共享或系统执行的概要记录。"""

    id: str | None = Field(default=None, alias="_id")
    execution_id: str = Field(default_factory=_uuid, min_length=1, max_length=100)
    trace_id: str = Field(..., min_length=1, max_length=100)
    task_id: str | None = Field(default=None, min_length=1, max_length=100)
    scope: Scope
    execution_type: ExecutionType
    status: RunStatus = RunStatus.PENDING

    owner_user_id: str | None = Field(default=None, min_length=1, max_length=100)
    initiator_user_id: str | None = Field(default=None, min_length=1, max_length=100)
    participant_user_ids: list[str] = Field(default_factory=list, max_length=100)
    parent_execution_id: str | None = Field(default=None, min_length=1, max_length=100)
    shared_execution_id: str | None = Field(default=None, min_length=1, max_length=100)

    current_phase: Phase | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    error_code: ErrorCode | None = None
    error_message: str | None = Field(default=None, max_length=2000)
    next_sequence: int = Field(default=0, ge=0)

    started_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    expires_at: datetime = Field(default_factory=_run_expires_at)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_tenant_ownership(self) -> ExecutionRun:
        if self.scope == Scope.USER:
            if not self.owner_user_id:
                raise ValueError("owner_user_id is required for user scope")
            if self.initiator_user_id or self.participant_user_ids:
                raise ValueError("user scope cannot contain shared participant fields")
        elif self.scope == Scope.SHARED:
            if not self.initiator_user_id:
                raise ValueError("initiator_user_id is required for shared scope")
            if self.owner_user_id:
                raise ValueError("shared scope cannot have owner_user_id")
        elif self.owner_user_id or self.initiator_user_id or self.participant_user_ids:
            raise ValueError("system scope cannot contain user identity fields")
        return self

    def transition_to(self, target: RunStatus | str, *, at: datetime | None = None) -> None:
        """执行受控状态转换，并在终态补齐结束时间和耗时。"""

        self.status = validate_run_status_transition(self.status, target)
        if self.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            completed_at = at or _utc_now()
            self.completed_at = completed_at
            self.duration_ms = max(0, int((completed_at - self.started_at).total_seconds() * 1000))


class ExecutionEvent(BaseModel):
    """一次执行中的 append-only 详细事件。"""

    id: str | None = Field(default=None, alias="_id")
    event_id: str = Field(default_factory=_uuid, min_length=1, max_length=100)
    execution_id: str = Field(..., min_length=1, max_length=100)
    trace_id: str = Field(..., min_length=1, max_length=100)
    task_id: str | None = Field(default=None, min_length=1, max_length=100)
    sequence: int = Field(..., ge=1)

    scope: Scope
    owner_user_id: str | None = Field(default=None, min_length=1, max_length=100)
    initiator_user_id: str | None = Field(default=None, min_length=1, max_length=100)
    service: LogService
    component: str = Field(..., min_length=1, max_length=100)
    phase: Phase
    action: Action
    event_type: EventType
    level: EventLevel = EventLevel.INFO
    message: str = Field(..., min_length=1, max_length=5000)

    span_id: str | None = Field(default=None, min_length=1, max_length=100)
    parent_span_id: str | None = Field(default=None, min_length=1, max_length=100)
    duration_ms: int | None = Field(default=None, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime = Field(default_factory=_event_expires_at)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_tenant_ownership_and_action(self) -> ExecutionEvent:
        if self.scope == Scope.USER and not self.owner_user_id:
            raise ValueError("owner_user_id is required for user event")
        if self.scope == Scope.SHARED and not self.initiator_user_id:
            raise ValueError("initiator_user_id is required for shared event")
        if self.scope == Scope.SYSTEM and (self.owner_user_id or self.initiator_user_id):
            raise ValueError("system event cannot contain user identity fields")
        if self.scope == Scope.USER and self.initiator_user_id:
            raise ValueError("user event cannot have initiator_user_id")
        if self.scope == Scope.SHARED and self.owner_user_id:
            raise ValueError("shared event cannot have owner_user_id")

        from execution_logs.catalog import action_spec

        spec = action_spec(self.action)
        if self.phase != spec.phase:
            raise ValueError(f"action {self.action.value} must use phase {spec.phase.value}")
        if self.scope not in spec.allowed_scopes:
            raise ValueError(
                f"action {self.action.value} is not allowed in {self.scope.value} scope"
            )
        return self


class ExecutionLink(BaseModel):
    """当前用户与一份共享执行之间的鉴权关系。"""

    id: str | None = Field(default=None, alias="_id")
    link_id: str = Field(default_factory=_uuid, min_length=1, max_length=100)
    user_id: str = Field(..., min_length=1, max_length=100)
    task_id: str = Field(..., min_length=1, max_length=100)
    user_execution_id: str = Field(..., min_length=1, max_length=100)
    shared_execution_id: str = Field(..., min_length=1, max_length=100)
    relation: Relation
    joined_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime = Field(default_factory=_run_expires_at)

    model_config = {"populate_by_name": True}
