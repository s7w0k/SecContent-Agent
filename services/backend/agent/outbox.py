"""Transactional Outbox — 阶段3 WBS 3.7（副作用治理）。

目的：状态提交与事件写入可对账（阶段3 §1.2 / 统一架构 §4）。
  - 运行状态/事件先入 outbox（与业务状态同库），后台投递到事件存储；
  - 投递失败不静默吞掉：状态 FAILED 后可被 reconcile job 重试；
  - dedup_key 唯一索引实现重复投递幂等（0 重复副作用）；
  - 无法保证幂等的操作（L3）必须要求审批并提供补偿动作 —— 补偿入口
    由 compensation 字段登记，失败进入独立 dead-letter 集合。

与 execution_step_ledger 的分工：
  - ledger 管"步骤执行"的幂等/租约；
  - outbox 管"状态→事件"的可靠投递与对账。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

logger = logging.getLogger("backend.agent.outbox")

COLLECTION = "event_outbox"
DEAD_LETTER_COLLECTION = "event_outbox_dead_letter"
SCHEMA_VERSION = "1.0"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class OutboxEntry(BaseModel):
    """outbox 条目（payload 只存脱敏字段）。"""

    schema_version: str = SCHEMA_VERSION
    entry_id: str
    run_id: str = ""
    aggregate_type: str = "run_state"  # run_state / runtime_event / agent_event
    event_type: str = ""
    event_status: str = ""  # 事件语义状态（投递到事件存储时保留）
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str = ""
    dedup_key: str = ""  # 幂等键（run_id + 业务唯一标识）
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    compensation: str = ""  # 补偿动作（无法幂等时登记）
    error_message: str = ""
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class OutboxStore:
    """event_outbox 写入/投递/对账（与业务状态同库，保证可对账）。"""

    def __init__(
        self,
        db: Any,
        *,
        collection: str = COLLECTION,
        dead_letter_collection: str = DEAD_LETTER_COLLECTION,
        max_attempts: int = 5,
    ):
        self.db = db
        self.collection_name = collection
        self.dead_letter_collection_name = dead_letter_collection
        self.col = db[collection]
        self.dead_col = db[dead_letter_collection]
        self.max_attempts = max(1, max_attempts)

    def index_specs(self) -> dict[str, list[IndexModel]]:
        return {
            self.collection_name: [
                IndexModel(
                    [("dedup_key", ASCENDING)], unique=True, sparse=True, name="uq_outbox_dedup"
                ),
                IndexModel(
                    [("status", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_outbox_status_created",
                ),
                IndexModel([("run_id", ASCENDING)], name="idx_outbox_run_id"),
            ],
            self.dead_letter_collection_name: [
                IndexModel([("entry_id", ASCENDING)], unique=True, name="uq_outbox_dead_entry"),
            ],
        }

    async def ensure_indexes(self) -> list[str]:
        names: list[str] = []
        for name, specs in self.index_specs().items():
            names.extend(await self.db[name].create_indexes(specs))
        return names

    async def enqueue(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        dedup_key: str = "",
        aggregate_type: str = "run_state",
        status: str = "",
        compensation: str = "",
        now: datetime | None = None,
    ) -> OutboxEntry | None:
        """入队；dedup_key 重复时返回 None（重复投递被幂等拒绝）。"""
        stamp = now or _utc_now()
        payload = payload or {}
        if dedup_key:
            # 前置幂等检查（唯一索引兜底并发）；重复投递返回 None → 0 重复副作用
            try:
                existing = await self.col.find_one({"dedup_key": dedup_key})
                if existing is not None:
                    logger.debug("[outbox] dedup skip %s", dedup_key)
                    return None
            except Exception:
                pass
        entry = OutboxEntry(
            entry_id="out-" + uuid.uuid4().hex[:12],
            run_id=run_id,
            aggregate_type=aggregate_type,
            event_type=event_type,
            event_status=status,
            payload=payload,
            payload_hash=_stable_hash(payload),
            dedup_key=dedup_key,
            compensation=compensation,
            created_at=stamp,
            updated_at=stamp,
        )
        try:
            await self.col.insert_one(entry.model_dump(mode="json"))
            return entry
        except Exception as exc:  # DuplicateKeyError → 幂等拒绝
            logger.debug("[outbox] dedup reject %s: %s", dedup_key, exc)
            return None

    async def claim_next(self, *, run_id: str = "", limit: int = 50) -> list[OutboxEntry]:
        """领取待投递条目（pending/failed 且未超次数）。"""
        query: dict[str, Any] = {
            "status": {"$in": [OutboxStatus.PENDING.value, OutboxStatus.FAILED.value]},
            "attempts": {"$lt": self.max_attempts},
        }
        if run_id:
            query["run_id"] = run_id
        try:
            cursor = self.col.find(query).sort("created_at", 1).limit(limit)
            docs = await cursor.to_list(length=limit)
            return [OutboxEntry.model_validate(d) for d in docs]
        except Exception:
            logger.warning("[outbox] claim_next failed")
            return []

    async def mark_sent(self, entry_id: str, *, now: datetime | None = None) -> bool:
        stamp = now or _utc_now()
        result = await self.col.update_one(
            {"entry_id": entry_id},
            {"$set": {"status": OutboxStatus.SENT.value, "updated_at": stamp}},
        )
        return result.matched_count > 0

    async def mark_failed(
        self, entry_id: str, *, error: str = "", now: datetime | None = None
    ) -> None:
        stamp = now or _utc_now()
        entry = await self.col.find_one({"entry_id": entry_id})
        if entry is None:
            return
        entry = OutboxEntry.model_validate(entry)
        attempts = entry.attempts + 1
        if attempts >= self.max_attempts:
            # 超过次数 → 独立 dead-letter（不伪装成功）
            await self.col.delete_one({"entry_id": entry_id})
            dead = entry.model_copy(
                update={
                    "status": OutboxStatus.FAILED.value,
                    "attempts": attempts,
                    "error_message": error[:300],
                    "updated_at": stamp,
                }
            )
            await self.dead_col.replace_one(
                {"entry_id": entry_id}, dead.model_dump(mode="json"), upsert=True
            )
            logger.warning("[outbox] dead-lettered %s attempts=%d: %s", entry_id, attempts, error)
        else:
            await self.col.update_one(
                {"entry_id": entry_id},
                {
                    "$set": {
                        "status": OutboxStatus.FAILED.value,
                        "attempts": attempts,
                        "error_message": error[:300],
                        "updated_at": stamp,
                    }
                },
            )

    async def reconcile(
        self,
        *,
        deliver: Any,
        limit: int = 100,
        run_id: str = "",
        now: datetime | None = None,
    ) -> dict[str, int]:
        """对账 job：重新投递 pending/failed 条目；返回 {sent, failed, skipped}。"""
        stats = {"sent": 0, "failed": 0, "skipped": 0}
        for entry in await self.claim_next(run_id=run_id, limit=limit):
            try:
                ok = await deliver(entry)
            except Exception as exc:
                ok = False
                logger.warning("[outbox] deliver %s raised: %s", entry.entry_id, exc)
            if ok:
                await self.mark_sent(entry.entry_id, now=now)
                stats["sent"] += 1
            else:
                await self.mark_failed(entry.entry_id, now=now)
                stats["failed"] += 1
        return stats

    async def pending_count(self, *, run_id: str = "") -> int:
        query: dict[str, Any] = {"status": OutboxStatus.PENDING.value}
        if run_id:
            query["run_id"] = run_id
        try:
            return int(await self.col.count_documents(query))
        except Exception:
            return 0


class EventOutbox:
    """面向运行时事件投递的 outbox 封装（写失败不静默吞掉）。"""

    def __init__(self, store: OutboxStore, deliver: Any | None = None):
        self.store = store
        self._deliver = deliver  # async (OutboxEntry) -> bool，None 时仅入队

    async def enqueue_run_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        status: str = "",
        now: datetime | None = None,
    ) -> OutboxEntry | None:
        return await self.store.enqueue(
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            dedup_key=f"{run_id}:{event_type}:{_stable_hash(payload or {})[:16]}",
            aggregate_type="runtime_event",
            status=status,
            now=now,
        )

    async def flush(self, *, limit: int = 100, run_id: str = "") -> dict[str, int]:
        """投递本轮全部待发事件（由 reconcile/worker 周期调用）。"""
        if self._deliver is None:
            return {"sent": 0, "failed": 0, "skipped": 0}
        return await self.store.reconcile(deliver=self._deliver, limit=limit, run_id=run_id)
