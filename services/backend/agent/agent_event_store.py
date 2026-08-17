"""Agent Loop 事件存储 — 阶段1 WBS 1.6（统一 EventEnvelope）。

对齐 00-统一架构 第 4 节事件契约：
  - 所有执行模式使用统一 EventEnvelope（schema_version / event_id / run_id /
    trace_id / parent_event_id / sequence / phase / event_type / step_id /
    attempt / status / model_id / tool_name / input_hash / result_hash /
    evidence_ids / token_usage / cost_usd / duration_ms / reason_code / timestamp）；
  - 阶段 1 补齐 tool_started / tool_finished / tool_failed / budget_reserved /
    budget_settled / finalization 事件；
  - 写入 agent_run_events 集合（索引在 db/mongo.py ensure_indexes 中登记）；
  - 事件只保存指纹与脱敏信息，不保存 prompt、完整工具参数/结果、密钥或私有思维链；
  - 写入失败仅记日志，绝不抛出（不影响 Loop 执行）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from agent.contracts.events import CORE_EVENT_TYPES, EventEnvelope, sanitize_event_payload
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

logger = logging.getLogger("backend.agent.agent_event_store")

COLLECTION = "agent_run_events"
SCHEMA_VERSION = "1.0"

Phase = Literal[
    "admit",
    "load_context",
    "think",
    "budget",
    "policy",
    "execute",
    "observe",
    "validate",
    "checkpoint",
    "replan",
    "finalize",
    "stop",
]

# 阶段1 要求补齐的事件类型
EVENT_TYPES = CORE_EVENT_TYPES | frozenset(
    {
        "loop_started",
        "loop_ended",
        "phase_changed",
        "think_started",
        "think_finished",
        "think_failed",
        "tool_started",
        "tool_finished",
        "tool_failed",
        "tool_blocked",
        "budget_reserved",
        "budget_settled",
        "budget_warning",
        "budget_released",
        "budget_denied",
        "budget_exhausted",
        "finalization_started",
        "finalization_finished",
        "validate_failed",
        "replanned",
        "loop_stopped",
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _event_id(run_id: str, sequence: int, event_type: str) -> str:
    raw = json.dumps(
        {"run_id": run_id, "sequence": sequence, "event_type": event_type},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "ev-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class AgentEventEnvelope(EventEnvelope):
    """统一 Agent Loop 事件信封（脱敏，可落库）。"""

    parent_event_id: str = Field(default="", max_length=64)
    phase: Phase = "execute"
    step_id: str = Field(default="", max_length=64)
    attempt: int = Field(default=1, ge=0)
    model_id: str = Field(default="", max_length=100)
    tool_name: str = Field(default="", max_length=100)
    input_hash: str = Field(default="", max_length=100)
    result_hash: str = Field(default="", max_length=100)
    evidence_ids: list[str] = Field(default_factory=list)
    token_usage: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = Field(default=0.0)
    duration_ms: int = Field(default=0, ge=0)
    reason_code: str = Field(default="", max_length=100)
    created_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime = Field(default_factory=lambda: _utc_now() + timedelta(days=30))


class AgentEventStore:
    """agent_run_events 写入/读取器：fire-and-forget，失败不影响执行。"""

    def __init__(self, db: Any, *, collection: str = COLLECTION, expires_days: int = 30):
        self.db = db
        self.collection_name = collection
        self.col = db[collection]
        self.expires_days = max(1, expires_days)
        self._seq: dict[str, int] = {}

    def index_specs(self) -> dict[str, list[IndexModel]]:
        """事件索引（与 db/mongo.py ensure_indexes 保持一致）。"""
        return {
            self.collection_name: [
                IndexModel(
                    [("run_id", ASCENDING), ("sequence", ASCENDING)],
                    unique=True,
                    name="idx_agent_event_run_sequence",
                ),
                IndexModel(
                    [("trace_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_agent_event_trace_created",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_agent_event_user_created",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="idx_agent_event_expires",
                ),
            ]
        }

    async def ensure_indexes(self) -> list[str]:
        return await self.col.create_indexes(self.index_specs()[self.collection_name])

    def next_sequence(self, run_id: str) -> int:
        """进程内单调递增序号（仅展示顺序，API 按 created_at 排序）。"""
        seq = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = seq
        return seq

    async def emit(
        self,
        *,
        run_id: str,
        event_type: str,
        trace_id: str = "",
        turn_id: str = "",
        parent_event_id: str = "",
        phase: Phase = "execute",
        step_id: str = "",
        attempt: int = 1,
        status: str = "",
        model_id: str = "",
        tool_name: str = "",
        input_hash: str = "",
        result_hash: str = "",
        evidence_ids: list[str] | None = None,
        token_usage: dict[str, int] | None = None,
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        reason_code: str = "",
        extra: dict[str, Any] | None = None,
        sequence: int | None = None,
    ) -> AgentEventEnvelope | None:
        """写入一条事件；任何失败仅记日志，绝不抛出。"""
        if event_type not in EVENT_TYPES:
            event_type = "loop_event"
        try:
            seq = sequence if sequence is not None else self.next_sequence(run_id)
            now = _utc_now()
            payload: dict[str, Any] = dict(extra or {})
            envelope = AgentEventEnvelope(
                event_id=_event_id(run_id, seq, event_type),
                run_id=run_id,
                trace_id=trace_id,
                turn_id=turn_id,
                parent_event_id=parent_event_id,
                sequence=seq,
                phase=phase,
                event_type=event_type,
                step_id=step_id,
                attempt=attempt,
                status=status,
                model_id=model_id,
                tool_name=tool_name,
                input_hash=input_hash,
                result_hash=result_hash,
                evidence_ids=list(evidence_ids or []),
                token_usage=dict(token_usage or {}),
                cost_usd=round(float(cost_usd), 8),
                duration_ms=max(0, int(duration_ms)),
                reason_code=reason_code,
                payload=sanitize_event_payload(payload),
                timestamp=now,
                created_at=now,
                expires_at=now + timedelta(days=self.expires_days),
            )
            doc = envelope.model_dump(mode="json")
            await self.col.insert_one(doc)
            return envelope
        except Exception:
            logger.warning(
                "[agent-events] emit %s failed (run=%s)", event_type, run_id, exc_info=True
            )
            return None

    async def list_run_events(self, run_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """按 created_at 读取一个 run 的事件流（供追溯/API）。"""
        try:
            cursor = self.col.find({"run_id": run_id}).sort("created_at", 1).limit(limit)
            docs = await cursor.to_list(length=limit)
            return list(docs)
        except Exception:
            logger.warning("[agent-events] list_run_events failed for %s", run_id, exc_info=True)
            return []
