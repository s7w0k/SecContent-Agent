"""PipelineEvent — 阶段三 Step 9。

MultiAgent 编排的观测事件流：
  plan_requested/created/rejected/fallback、
  step_scheduled/worker_started/retrying/succeeded/failed/step_skipped/
  dead_lettered/replayed、run_finished/run_canceled。

字段：run/plan/step/worker/version/attempt/sequence/input_hash/result_hash/
      queue_ms/duration/error_type/status。

容量控制：
  - 全部事件按 TTL 归档（默认 90 天），成功事件不做采样削减；
  - sequence 为进程内单调递增（仅用于展示顺序，API 按 created_at 排序）；
  - 不保存完整 prompt/工具全文/业务正文（事件只存指纹与脱敏错误）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

logger = logging.getLogger("backend.agent.events")

COLLECTION = "pipeline_events"

EventType = Literal[
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
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PipelineEvent(BaseModel):
    """单条 MultiAgent 观测事件（脱敏，无业务正文）。"""

    event_type: EventType
    run_id: str = Field(default="", min_length=1, max_length=100)
    plan_id: str = Field(default="", max_length=100)
    step_id: str = Field(default="", max_length=64)
    worker: str = Field(default="", max_length=64)
    version: str = Field(default="", max_length=64)
    attempt: int = Field(default=0, ge=0)
    sequence: int = Field(default=0, ge=0)
    input_hash: str = Field(default="", max_length=100)
    result_hash: str = Field(default="", max_length=100)
    queue_ms: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    error_type: str | None = Field(default=None, max_length=100)
    status: str = Field(default="", max_length=32)
    created_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime = Field(default_factory=lambda: _utc_now() + timedelta(days=90))


class EventEmitter:
    """pipeline_events 写入器：fire-and-forget，失败不影响流水线执行。"""

    def __init__(
        self,
        db: Any,
        *,
        collection: str = COLLECTION,
        expires_days: int = 90,
    ):
        self.db = db
        self.col = db[collection]
        self.collection_name = collection
        self.expires_days = max(1, expires_days)
        # 进程内单调递增：planner→orchestrator→replay 同进程内有序
        self._seq: dict[str, int] = {}

    def index_specs(self) -> dict[str, list[IndexModel]]:
        """事件索引（与 db/mongo.py ensure_indexes 保持一致）。"""
        return {
            self.collection_name: [
                # 按 run 读取事件流
                IndexModel(
                    [("run_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_pipeline_events_run_created",
                ),
                # 按类型聚合/指标
                IndexModel(
                    [("event_type", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_pipeline_events_type_created",
                ),
                # TTL/归档（成功事件不做采样，靠 TTL 与聚合控制容量）
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="ttl_pipeline_events_expires",
                ),
            ]
        }

    async def ensure_indexes(self) -> list[str]:
        return await self.db[self.collection_name].create_indexes(
            self.index_specs()[self.collection_name]
        )

    async def emit(
        self,
        *,
        event_type: EventType,
        run_id: str = "",
        plan_id: str = "",
        step_id: str = "",
        worker: str = "",
        version: str = "",
        attempt: int = 0,
        input_hash: str = "",
        result_hash: str = "",
        queue_ms: int = 0,
        duration_ms: int = 0,
        error_type: str | None = None,
        status: str = "",
    ) -> PipelineEvent | None:
        """写入一条事件；任何失败仅记日志，绝不抛出。"""
        try:
            seq = self._seq.get(run_id, 0) + 1
            self._seq[run_id] = seq
            now = _utc_now()
            event = PipelineEvent(
                event_type=event_type,
                run_id=run_id,
                plan_id=plan_id,
                step_id=step_id,
                worker=worker,
                version=version,
                attempt=attempt,
                sequence=seq,
                input_hash=input_hash,
                result_hash=result_hash,
                queue_ms=queue_ms,
                duration_ms=duration_ms,
                error_type=error_type,
                status=status,
                created_at=now,
                expires_at=now + timedelta(days=self.expires_days),
            )
            await self.col.insert_one(event.model_dump(mode="json"))
            return event
        except Exception:
            logger.warning("[events] emit %s failed (run=%s step=%s)", event_type, run_id, step_id)
            return None

    async def list_run_events(self, run_id: str, *, limit: int = 500) -> list[PipelineEvent]:
        """按 created_at 读取一个 run 的事件流（供 API/前端树形视图）。"""
        try:
            cursor = self.col.find({"run_id": run_id}).sort("created_at", 1).limit(limit)
            docs = await cursor.to_list(length=limit)
            return [PipelineEvent.model_validate(d) for d in docs]
        except Exception:
            logger.warning("[events] list_run_events failed for %s", run_id)
            return []
