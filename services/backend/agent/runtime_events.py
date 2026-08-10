"""RuntimeEvent 与 RuntimeEventStore — 阶段四 4A Step 4A-8 / 4A-9。

自主运行的事件流（SSE 数据源）：
  - 事件统一包含 schema_version、event_id、sequence、run_id 和时间戳；
  - sequence 在 run 内单调递增，支持 Last-Event-ID 断线续传；
  - run_id + sequence 唯一索引防重放；
  - expires_at TTL 归档（默认 30 天，来自 Settings.AUTONOMOUS_EVENT_TTL_DAYS）；
  - payload 只保存脱敏信息（决策摘要、原因码、用量），不保存参数/提示词/密钥原文。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, IndexModel

logger = logging.getLogger("backend.agent.runtime_events")

COLLECTION = "runtime_events"
SCHEMA_VERSION = "1.0"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _event_id(run_id: str, sequence: int, event_type: str) -> str:
    raw = json.dumps(
        {"run_id": run_id, "sequence": sequence, "event_type": event_type},
        sort_keys=True, separators=(",", ":"),
    )
    return "ev-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class RuntimeEvent(BaseModel):
    """单条自主运行事件（脱敏，供 SSE/审计）。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = SCHEMA_VERSION
    event_id: str
    sequence: int = Field(ge=1)
    run_id: str = Field(..., min_length=1, max_length=100)
    event_type: str = Field(..., min_length=1, max_length=64)
    status: str = Field(default="", max_length=32)
    timestamp: datetime = Field(default_factory=_utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime = Field(default_factory=lambda: _utc_now() + timedelta(days=30))


class RuntimeEventStore:
    """runtime_events 写入/读取器：append 分配递增 sequence，支持断线续传。"""

    def __init__(self, db: Any, *, collection: str = COLLECTION, expires_days: int = 30):
        self.db = db
        self.collection_name = collection
        self.col = db[collection]
        self.expires_days = max(1, expires_days)

    def index_specs(self) -> dict[str, list[IndexModel]]:
        """事件索引（与 db/mongo.py ensure_indexes 保持一致）。"""
        return {
            self.collection_name: [
                # run 内 sequence 唯一：防重放 + 续传锚点
                IndexModel(
                    [("run_id", ASCENDING), ("sequence", ASCENDING)],
                    unique=True,
                    name="uq_runtime_event_run_seq",
                ),
                # 按 run 读取事件流（续传/审计）
                IndexModel(
                    [("run_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_runtime_event_run_created",
                ),
                # TTL 归档
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="ttl_runtime_events_expires",
                ),
            ]
        }

    async def ensure_indexes(self) -> list[str]:
        return await self.col.create_indexes(self.index_specs()[self.collection_name])

    async def append(
        self,
        *,
        run_id: str,
        event_type: str,
        status: str = "",
        payload: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> RuntimeEvent | None:
        """追加一条事件；sequence = 当前 run 最大序号 + 1（唯一索引防并发冲突）。"""
        stamp = now or _utc_now()
        for _attempt in range(3):
            last = await self._last_sequence(run_id)
            sequence = last + 1
            event = RuntimeEvent(
                event_id=_event_id(run_id, sequence, event_type),
                sequence=sequence,
                run_id=run_id,
                event_type=event_type,
                status=status,
                timestamp=stamp,
                payload=payload or {},
                created_at=stamp,
                expires_at=stamp + timedelta(days=self.expires_days),
            )
            try:
                await self.col.insert_one(event.model_dump(mode="json"))
                return event
            except Exception as exc:  # DuplicateKeyError → 重取序号
                if _attempt >= 2:
                    logger.warning("[runtime_events] append conflict for %s seq=%d: %s", run_id, sequence, exc)
                    return None
                logger.debug("[runtime_events] sequence conflict, retrying run=%s", run_id)
        return None

    async def read_after_sequence(self, run_id: str, last_sequence: int = 0, *, limit: int = 500) -> list[RuntimeEvent]:
        """断线续传：返回 sequence > last_sequence 的事件（升序）。"""
        try:
            cursor = (
                self.col.find({"run_id": run_id, "sequence": {"$gt": last_sequence}})
                .sort("sequence", 1)
                .limit(limit)
            )
            docs = await cursor.to_list(length=limit)
            return [RuntimeEvent.model_validate(d) for d in docs]
        except Exception:
            logger.warning("[runtime_events] read_after_sequence failed for %s", run_id)
            return []

    async def list_run_events(self, run_id: str, *, limit: int = 500) -> list[RuntimeEvent]:
        """按序号读取一个 run 的完整事件流。"""
        try:
            cursor = self.col.find({"run_id": run_id}).sort("sequence", 1).limit(limit)
            docs = await cursor.to_list(length=limit)
            return [RuntimeEvent.model_validate(d) for d in docs]
        except Exception:
            logger.warning("[runtime_events] list_run_events failed for %s", run_id)
            return []

    async def _last_sequence(self, run_id: str) -> int:
        try:
            doc = await self.col.find_one(
                {"run_id": run_id}, sort=[("sequence", -1)]
            )
            return int((doc or {}).get("sequence", 0))
        except Exception:
            return 0
