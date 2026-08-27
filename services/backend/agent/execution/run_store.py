"""ExecutionRunStore - skill_planned Durable Resume（Final Closure 计划 EPIC-A §5 / §6 / §7 / §8）。

不复制旧 LangGraph checkpoint；采用"Execution Ledger + ArtifactRef + Skill Step State + Idempotency"
实现幂等恢复（§4）。Worker crash → retry/resume → 从 ExecutionRunRecord 恢复已完成的 step，
跳过已完成的 Skill 并复用其 Artifact，绝不多写高风险的写副作用。

Mongo Collection：``agent_execution_runs``（schema §6）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

RunStatus = Literal["PLANNING", "RUNNING", "COMPLETED", "FAILED", "BLOCKED"]


class ExecutionRunRecord(BaseModel):
    """一次 skill_planned 执行的持久化快照（计划 §6）。"""

    run_id: str
    task_id: str

    execution_engine: str = "skill_planned"
    execution_mode: str = "skill_planned"

    user_id: str = ""
    tenant_id: str = ""

    goal: str = ""
    intent: str = "full_workflow"

    plan_id: str = ""
    plan_version: str = "1.0.0"

    skill_snapshot_hash: str = ""
    wiki_version: str = ""

    status: RunStatus = "RUNNING"

    completed_steps: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)

    artifact_refs: dict[str, str] = Field(default_factory=dict)

    current_step: str | None = None
    reviewer_rounds: int = Field(default=0, ge=0)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


RUN_INDEXES: list[IndexModel] = [
    IndexModel([("run_id", ASCENDING)], unique=True, name="uq_exec_run_id"),
    IndexModel([("task_id", ASCENDING)], unique=True, name="uq_exec_run_task"),
    IndexModel(
        [("tenant_id", ASCENDING), ("created_at", DESCENDING)],
        name="idx_exec_run_tenant_created",
    ),
    IndexModel(
        [("status", ASCENDING), ("updated_at", DESCENDING)],
        name="idx_exec_run_status_updated",
    ),
]


class ResumeStateNotFound(RuntimeError):  # noqa: N818 - 计划 §14 保持一致命名
    """Resume 时找不到已持久化的 ExecutionRunRecord（§14）。"""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"resume state not found for task: {task_id}")


class ExecutionRunStore:
    """以 ExecutionRunRecord 持久化每次 skill_planned 执行的步进状态。"""

    COLLECTION = "agent_execution_runs"

    def __init__(self, db: Any) -> None:
        self._db = db

    @property
    def collection(self) -> Any:
        return self._db[self.COLLECTION]

    async def ensure_indexes(self) -> list[str]:
        return await self.collection.create_indexes(RUN_INDEXES)

    # ── 读 ─────────────────────────────────────────────

    async def get_by_task(self, task_id: str) -> ExecutionRunRecord | None:
        return self._from_doc(await self.collection.find_one({"task_id": task_id}))

    async def get_by_run_id(self, run_id: str) -> ExecutionRunRecord | None:
        return self._from_doc(await self.collection.find_one({"run_id": run_id}))

    # ── 写（read-modify-write，run_id 为稳定主键）────────────

    async def create_run(self, record: ExecutionRunRecord) -> None:
        # python 模式保留 datetime 原类型，Mongo 存为 BSON Date，读回可正确重建 record。
        data = record.model_dump()
        data["_id"] = record.run_id
        await self.collection.replace_one({"run_id": record.run_id}, data, upsert=True)

    async def mark_step_started(self, run_id: str, step_id: str) -> None:
        await self._mutate(
            run_id,
            mutate=lambda r: (setattr(r, "current_step", step_id), setattr(r, "status", "RUNNING")),
        )

    async def mark_step_completed(
        self, run_id: str, step_id: str, artifact_refs: list[str] | None = None
    ) -> None:
        refs = list(artifact_refs or [])

        def _apply(r: ExecutionRunRecord) -> None:
            if step_id not in r.completed_steps:
                r.completed_steps.append(step_id)
            if refs:
                r.artifact_refs[step_id] = refs[0]
            r.current_step = step_id
            if r.status in ("PLANNING", "RUNNING", "WAITING"):
                r.status = "RUNNING"

        await self._mutate(run_id, mutate=_apply)

    async def mark_step_failed(self, run_id: str, step_id: str) -> None:
        await self._mutate(
            run_id,
            mutate=lambda r: (r.failed_steps.append(step_id) if step_id not in r.failed_steps else None),
        )

    async def mark_completed(self, run_id: str) -> None:
        await self._set_status(run_id, "COMPLETED")

    async def mark_failed(self, run_id: str) -> None:
        await self._set_status(run_id, "FAILED")

    async def mark_blocked(self, run_id: str) -> None:
        await self._set_status(run_id, "BLOCKED")

    # ── 内部 ───────────────────────────────────────────

    async def _set_status(self, run_id: str, status: RunStatus) -> None:
        await self._mutate(run_id, mutate=lambda r: setattr(r, "status", status))

    async def _mutate(
        self, run_id: str, *, mutate: Any
    ) -> None:
        record = await self.get_by_run_id(run_id)
        if record is None:
            return
        mutate(record)
        record.updated_at = datetime.now(UTC)
        await self.create_run(record)

    @staticmethod
    def _from_doc(doc: dict[str, Any] | None) -> ExecutionRunRecord | None:
        if doc is None:
            return None
        return ExecutionRunRecord(**{k: v for k, v in doc.items() if k != "_id"})


__all__ = [
    "RUN_INDEXES",
    "ExecutionRunRecord",
    "ExecutionRunStore",
    "ResumeStateNotFound",
    "RunStatus",
]
