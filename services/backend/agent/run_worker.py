"""Autonomous 队列化 durable worker — 阶段3 WBS 3.4（跨进程恢复）。

执行模型（阶段3 §2.1）：
```
API create run
  -> persist manifest/state
  -> enqueue run_id
  -> runtime worker claims lease
  -> execute/checkpoint/heartbeat
  -> complete or release/dead-letter
```

DurableRunExecutor 是可独立测试的执行器：
  - 领取 run 租约（LeaseConflictError → 跳过，不并发执行）；
  - 运行 AgentRuntime，每次 checkpoint 同步心跳续期（heartbeat）；
  - 终态保存后释放租约；异常时终态化（internal_error）或释放租约供重试；
  - recover_stale 复用 RunReaper 扫描（不允许重启后永久 running）。

ARQ worker 入口 execute_autonomous_run 为薄封装，通过 context 注入依赖。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agent.run_lease import LeaseConflictError, RunLeaseStore
from agent.runtime_state import RuntimeState, RuntimeStatus
from agent.runtime_store import RuntimeStateStore

logger = logging.getLogger("backend.agent.run_worker")

SCHEMA_VERSION = "1.0"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunWorkerResult:
    run_id: str
    executed: bool
    status: str = ""
    reason: str = ""
    lease_conflict: bool = False
    recovered: bool = False
    steps_run: int = 0


# runtime_factory 签名：Callable[[RuntimeState, checkpointer], AgentRuntime]
#   checkpointer: Callable[[RuntimeState], Awaitable[None]] —— worker 注入的心跳版检查点
RuntimeFactory = Callable[[RuntimeState, Callable[[RuntimeState], Awaitable[None]]], Any]


class DurableRunExecutor:
    """run 租约 + 心跳 + 终态化的 durable 执行器。"""

    def __init__(
        self,
        *,
        state_store: RuntimeStateStore,
        lease_store: RunLeaseStore,
        runtime_factory: RuntimeFactory,
        owner_id: str = "worker",
    ):
        self.state_store = state_store
        self.lease_store = lease_store
        self.runtime_factory = runtime_factory
        self.owner_id = owner_id

    async def execute(
        self,
        run_id: str,
        user_id: str,
        *,
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> RunWorkerResult:
        """durable 执行：lease → run(带心跳) → 终态保存 → release。"""
        stamp = now or _utc_now()
        owner = owner_id or self.owner_id
        state = await self.state_store.load(run_id)
        if state is None or state.user_id != user_id:
            return RunWorkerResult(
                run_id=run_id, executed=False, reason="run not found or forbidden"
            )
        if state.is_terminal:
            return RunWorkerResult(
                run_id=run_id,
                executed=False,
                status=state.status.value,
                reason="terminal state, skipped",
                steps_run=state.usage.steps,
            )

        # 1. 领取租约（被他人持有 → 跳过）
        try:
            lease = await self.lease_store.acquire(run_id, owner, now=stamp)
        except LeaseConflictError:
            return RunWorkerResult(
                run_id=run_id,
                executed=False,
                reason="lease held by another worker",
                lease_conflict=True,
            )

        # 2. 心跳版 checkpointer：每次 checkpoint 续期租约（心跳必须随时间推进，
        #    否则长任务在 start+ttl 后会被 reaper 误回收）
        async def _checkpoint(s: RuntimeState) -> None:
            await self.state_store.save(s)
            renewed = await self.lease_store.renew(run_id, owner, lease.fencing_token)
            if renewed is None:
                logger.warning("[run_worker] heartbeat lost for %s", run_id)

        recovered = bool(state.completed_steps)

        try:
            # 3. 构建并执行（runtime_factory 允许同步返回或 async 返回 AgentRuntime；
            #    组装错误如缺 planner/executor 同样走 4b 终态化，不允许遗留 running）
            runtime = self.runtime_factory(state, _checkpoint)
            if asyncio.iscoroutine(runtime):
                runtime = await runtime
            result = await runtime.run(state, cancel_event=None, now=stamp)
            await self.state_store.save(result.final_state)
            status = result.status.value
            reason = result.reason_code or ""
            logger.info("[run_worker] run %s finished: %s", run_id, status)
        except asyncio.CancelledError:
            # 4a. 取消：释放租约，状态保持可恢复
            await self.lease_store.release(run_id, owner, lease.fencing_token)
            raise
        except Exception as exc:
            # 4b. 未分类异常：不允许遗留 running（终态化 failed 或释放租约）
            logger.exception("[run_worker] run %s unexpected error", run_id)
            try:
                failed, _ = state.transition_to(
                    RuntimeStatus.FAILED,
                    reason="unclassified worker error",
                    actor=owner,
                    reason_code="internal_error",
                    now=stamp,
                )
                await self.state_store.save(failed)
                status = failed.status.value
                reason = "internal_error"
            except Exception:
                await self.lease_store.release(run_id, owner, lease.fencing_token)
                status = RuntimeStatus.RUNNING.value
                reason = f"unrecoverable: {exc}"
            logger.warning("[run_worker] run %s terminalized: %s", run_id, reason)
        else:
            # 5. 成功/终态：释放租约
            await self.lease_store.release(run_id, owner, lease.fencing_token)

        return RunWorkerResult(
            run_id=run_id,
            executed=True,
            status=status,
            reason=reason,
            recovered=recovered,
            steps_run=state.usage.steps,
        )

    async def recover_stale(
        self, *, owner_id: str | None = None, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """启动扫描：终态化租约过期但状态仍 running 的 run（不允许永久 running）。"""
        from agent.run_lease import RunReaper

        reaper = RunReaper(self.state_store, self.lease_store)
        return await reaper.scan_stale_running(owner_id=owner_id or self.owner_id, now=now)


def enqueue_autonomous_run(arq_pool: Any, *, run_id: str, user_id: str) -> bool:
    """入队 run_id（ARQ pool 可用时）；失败返回 False，由调用方降级。"""
    if arq_pool is None:
        return False
    try:
        arq_pool.enqueue_job("execute_autonomous_run", run_id, user_id)
        return True
    except Exception:
        logger.warning("[run_worker] enqueue %s failed", run_id)
        return False


async def execute_autonomous_run(
    ctx: dict[str, Any],
    run_id: str,
    user_id: str,
) -> dict[str, Any]:
    """ARQ worker 入口：从 context 构建依赖并 durable 执行。"""
    from agent.run_lease import RunLeaseStore
    from agent.runtime_store import RuntimeStateStore

    db = ctx.get("db")
    if db is None:
        return {"run_id": run_id, "executed": False, "reason": "no db in ctx"}
    settings = ctx.get("settings")
    state_store = RuntimeStateStore(db)
    lease_store = RunLeaseStore(db, ttl_seconds=getattr(settings, "AUTONOMOUS_LEASE_SECONDS", 120))

    service_factory = ctx.get("runtime_factory")
    if service_factory is None:
        return {"run_id": run_id, "executed": False, "reason": "no runtime_factory in ctx"}

    executor = DurableRunExecutor(
        state_store=state_store,
        lease_store=lease_store,
        runtime_factory=service_factory,
        owner_id="arq-worker",
    )
    result = await executor.execute(run_id, user_id)
    return {
        "run_id": result.run_id,
        "executed": result.executed,
        "status": result.status,
        "reason": result.reason,
        "lease_conflict": result.lease_conflict,
        "recovered": result.recovered,
        "steps_run": result.steps_run,
    }
