"""Run Lease / Heartbeat / Reaper — 阶段3 WBS 3.5（恢复组件）。

run-level 租约（阶段3 §2.2）：
  - lease、heartbeat、owner、expires_at、fencing token；
  - 启动时扫描 stale running（recover_after_restart）；
  - stuck-run reaper：lease 过期但状态仍 running/planning → 释放租约、
    写入 STOPPED（reason_code=lease_expired），不允许重启后永久 running；
  - 迟到写入（旧 fencing token）被租约层拒绝。

与 runtime_store 乐观锁（checkpoint_version CAS）互补：
  - checkpoint_version 防"旧执行器覆盖新状态"；
  - RunLease 防"两个执行器同时跑同一个 run"。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from agent.runtime_state import RuntimeStatus
from agent.runtime_store import RuntimeStateStore
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

logger = logging.getLogger("backend.agent.run_lease")

COLLECTION = "runtime_leases"
DEFAULT_TTL_SECONDS = 120


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LeaseConflictError(RuntimeError):
    """租约已被其他执行器持有（非过期）。"""


class RunLease(BaseModel):
    """run 级租约。"""

    run_id: str
    owner_id: str
    expires_at: datetime
    fencing_token: int = 1
    updated_at: datetime = Field(default_factory=_utc_now)

    @property
    def expired(self) -> bool:
        return _utc_now() >= self.expires_at


class RunLeaseStore:
    """runtime_leases 集合：acquire（CAS）/ renew（heartbeat）/ release。"""

    def __init__(self, db: Any, *, collection: str = COLLECTION, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.db = db
        self.collection_name = collection
        self.col = db[collection]
        self.ttl_seconds = max(1, ttl_seconds)

    def index_specs(self) -> dict[str, list[IndexModel]]:
        return {
            self.collection_name: [
                IndexModel([("run_id", ASCENDING)], unique=True, name="uq_lease_run_id"),
                IndexModel([("owner_id", ASCENDING)], name="idx_lease_owner"),
            ]
        }

    async def ensure_indexes(self) -> list[str]:
        return await self.col.create_indexes(self.index_specs()[self.collection_name])

    async def acquire(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> RunLease:
        """原子抢占租约：不存在或已过期 → 成功；被他人持有 → LeaseConflictError。"""
        stamp = now or _utc_now()
        ttl = ttl_seconds or self.ttl_seconds
        expires = stamp + timedelta(seconds=ttl)
        existing = await self.col.find_one({"run_id": run_id})
        if existing is not None:
            lease = RunLease.model_validate(existing)
            # 过期判断必须以调用方时间（now）为准：既支持确定性测试，
            # 也避免服务时钟与业务时钟漂移导致误判。租约未过期且被他人持有 → 冲突。
            if lease.expires_at > stamp and lease.owner_id != owner_id:
                raise LeaseConflictError(
                    f"run {run_id} lease held by {lease.owner_id} until {lease.expires_at}"
                )
            # 抢占（过期租约）：fencing token 递增，拒绝迟到写入
            token = lease.fencing_token + 1
            new_lease = RunLease(
                run_id=run_id,
                owner_id=owner_id,
                expires_at=expires,
                fencing_token=token,
                updated_at=stamp,
            )
            result = await self.col.replace_one(
                {"run_id": run_id, "fencing_token": lease.fencing_token},
                new_lease.model_dump(mode="json"),
                upsert=False,
            )
            if result.matched_count == 0:
                raise LeaseConflictError(f"run {run_id} lease contested (fencing token changed)")
            return new_lease
        new_lease = RunLease(
            run_id=run_id, owner_id=owner_id, expires_at=expires, fencing_token=1, updated_at=stamp
        )
        try:
            await self.col.insert_one(new_lease.model_dump(mode="json"))
        except Exception as exc:  # DuplicateKeyError → 并发抢占失败
            raise LeaseConflictError(f"run {run_id} lease race lost") from exc
        return new_lease

    async def renew(
        self,
        run_id: str,
        owner_id: str,
        fencing_token: int,
        *,
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> RunLease | None:
        """心跳续期：owner + fencing token 匹配才允许；否则返回 None。"""
        stamp = now or _utc_now()
        ttl = ttl_seconds or self.ttl_seconds
        expires = stamp + timedelta(seconds=ttl)
        result = await self.col.update_one(
            {
                "run_id": run_id,
                "owner_id": owner_id,
                "fencing_token": fencing_token,
            },
            {"$set": {"expires_at": expires, "updated_at": stamp}},
        )
        # matched_count：命中即续期成功（即便字段值相同，Mongo 也可能不产生修改）
        if result.matched_count == 0:
            return None
        return RunLease(
            run_id=run_id,
            owner_id=owner_id,
            expires_at=expires,
            fencing_token=fencing_token,
            updated_at=stamp,
        )

    async def release(
        self, run_id: str, owner_id: str, fencing_token: int
    ) -> bool:
        """释放租约（owner + fencing 匹配才删除）。"""
        result = await self.col.delete_one(
            {"run_id": run_id, "owner_id": owner_id, "fencing_token": fencing_token}
        )
        return result.deleted_count > 0

    async def load(self, run_id: str) -> RunLease | None:
        doc = await self.col.find_one({"run_id": run_id})
        return RunLease.model_validate(doc) if doc else None


class RunReaper:
    """stale running 扫描与恢复（启动 / 定时触发）。"""

    def __init__(self, state_store: RuntimeStateStore, lease_store: RunLeaseStore):
        self.state_store = state_store
        self.lease_store = lease_store

    async def scan_stale_running(
        self,
        *,
        owner_id: str = "reaper",
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """找出租约过期但状态仍 running/planning 的 run：
        释放租约 + 状态置 STOPPED（reason_code=lease_expired，不允许永久 running）。"""
        stamp = now or _utc_now()
        reaped: list[dict[str, Any]] = []
        states = await self.state_store.list_runs(limit=limit)
        for state in states:
            if state.status not in (RuntimeStatus.RUNNING, RuntimeStatus.PLANNING):
                continue
            lease = await self.lease_store.load(state.run_id)
            if lease is not None and lease.expires_at > stamp:
                continue  # 租约健康（以扫描时间 now 为准），跳过
            # 租约缺失或过期 → 终态化
            try:
                new_state, _ = state.transition_to(
                    RuntimeStatus.STOPPED,
                    reason="lease expired or missing; reaped on restart",
                    actor=owner_id,
                    reason_code="lease_expired",
                    now=stamp,
                )
                await self.state_store.save(new_state)
            except Exception as exc:
                logger.warning("[reaper] terminalize %s failed: %s", state.run_id, exc)
                continue
            if lease is not None:
                await self.lease_store.release(state.run_id, lease.owner_id, lease.fencing_token)
            reaped.append(
                {
                    "run_id": state.run_id,
                    "previous_status": state.status.value,
                    "reaped_at": stamp.isoformat(),
                }
            )
        return reaped

    async def recover_after_restart(
        self, *, owner_id: str = "reaper", now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """启动扫描：不允许重启后永久 running（调用 scan_stale_running）。"""
        return await self.scan_stale_running(owner_id=owner_id, now=now)
