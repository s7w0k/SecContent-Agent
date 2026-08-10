"""Orchestrator — 阶段三 Step 5。

从已验证 Plan 构建拓扑波次并按波次调度 Worker：
  - 无依赖步骤并行，有依赖按波次执行；
  - 全局 / 用户 / provider(并发组) / Worker 四层并发配额；
  - asyncio.gather(return_exceptions=True) 处理同波部分失败；
  - required 失败阻断依赖；optional/best_effort 失败按 policy 跳过或继续；
  - 支持 cancel、deadline、timeout、retry、dead-letter；
  - 租约含 owner_id/expires_at/fencing_token（fencing 语义由 Step 6 ledger 强制）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from agent.plan_contracts import PipelinePlan, PlanStep
from agent.worker_registry import WorkerLease, WorkerRegistry, WorkerResult
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.orchestrator")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OrchestratorError(Exception):
    """计划无法编排（环 / 依赖缺失）。"""


# ═══════════════════════════════════════════════════════════════
# 结果模型
# ═══════════════════════════════════════════════════════════════


class StepOutcome(BaseModel):
    step_id: str = Field(..., min_length=1, max_length=64)
    worker: str = Field(..., min_length=1, max_length=64)
    status: str = Field(..., description="succeeded/failed/skipped/dead_lettered/canceled")
    attempt: int = Field(default=1, ge=0)
    error_type: str | None = None
    error_message: str | None = Field(default=None, max_length=2000)
    idempotency_key: str = ""
    input_hash: str = ""
    result_hash: str = ""
    duration_ms: int = Field(default=0, ge=0)
    reason: str = ""


class RunOutcome(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=100)
    plan_id: str = Field(..., min_length=1, max_length=100)
    status: str = Field(..., description="completed/failed/canceled/partial")
    waves: int = Field(default=0, ge=0)
    steps: list[StepOutcome] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0)


# ═══════════════════════════════════════════════════════════════
# 拓扑波次
# ═══════════════════════════════════════════════════════════════


def build_waves(plan: PipelinePlan) -> list[list[PlanStep]]:
    """把 DAG 拆成拓扑波次：同一波内的步骤可并行执行。"""
    by_id = {s.step_id: s for s in plan.steps}
    remaining = set(by_id)
    done: set[str] = set()
    waves: list[list[PlanStep]] = []
    while remaining:
        wave = [
            by_id[sid]
            for sid in remaining
            if not by_id[sid].depends_on or set(by_id[sid].depends_on) <= done
        ]
        if not wave:
            raise OrchestratorError(
                f"plan cannot be scheduled: cycle or missing deps, remaining={sorted(remaining)}"
            )
        waves.append(wave)
        done |= {s.step_id for s in wave}
        remaining -= {s.step_id for s in wave}
    return waves


# ═══════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════


class Orchestrator:
    """按拓扑波次执行已验证 Plan。

    ``registry`` 提供 name→WorkerAdapter。所有状态迁移均在单进程内
    （CAS 的 Mongo 落地由 Step 6 ledger 承担），run 终态唯一：
    completed / failed / canceled / partial。
    partial = 存在死信步骤（重试耗尽、可人工重放），run 仍保留已确认产物。
    """

    def __init__(
        self,
        registry: WorkerRegistry,
        *,
        owner_id: str = "orchestrator",
        max_concurrency: int = 5,
        user_concurrency: int = 2,
        provider_concurrency: dict[str, int] | None = None,
        worker_concurrency: int = 2,
        lease_seconds: int = 120,
        default_max_attempts: int = 3,
        ledger: Any = None,
        emitter: Any = None,
    ):
        self.registry = registry
        self.owner_id = owner_id
        self.max_concurrency = max(1, max_concurrency)
        self.user_concurrency = max(1, user_concurrency)
        self.provider_concurrency = provider_concurrency or {"llm": 3, "crawl": 2, "local": 4}
        self.worker_concurrency = max(1, worker_concurrency)
        self.lease_seconds = max(1, lease_seconds)
        self.default_max_attempts = max(1, default_max_attempts)
        # 可选 Step Ledger：提供时步骤状态经 CAS 记账（begin/complete/fail），
        # 供恢复流程与人工重放使用；缺省时仅进程内执行。
        self.ledger = ledger
        # 可选事件发射器（Step 9 观测）：fire-and-forget，失败不影响执行
        self.emitter = emitter

        self._global_sem = asyncio.Semaphore(self.max_concurrency)
        self._user_sems: dict[str, asyncio.Semaphore] = {}
        self._provider_sems: dict[str, asyncio.Semaphore] = {}
        self._worker_sems: dict[str, asyncio.Semaphore] = {}
        self._sem_lock = asyncio.Lock()

    # ── 公开接口 ──────────────────────────────────────────────

    async def run(
        self,
        plan: PipelinePlan,
        *,
        state: dict[str, Any] | None = None,
        user_id: str = "",
        trace_id: str = "",
        cancel_event: asyncio.Event | None = None,
        deadline_at: float | None = None,
    ) -> RunOutcome:
        """执行计划。``deadline_at`` 使用 time.monotonic() 时间戳。"""
        started = time.perf_counter()
        run_id = plan.run_id
        plan_id = plan.plan_id
        state = state or {}
        waves = build_waves(plan)
        results: dict[str, StepOutcome] = {}
        canceled = False

        base_ctx = {
            "run_id": run_id,
            "plan_id": plan_id,
            "user_id": user_id,
            "trace_id": trace_id,
        }

        for wave_index, wave in enumerate(waves):
            if self._is_canceled(cancel_event, deadline_at):
                canceled = True
                break
            jobs: list[PlanStep] = []
            for step in wave:
                if not self._deps_ok(plan, step, results):
                    outcome = StepOutcome(
                        step_id=step.step_id,
                        worker=step.worker,
                        status="skipped",
                        reason="dependency failed",
                    )
                    results[step.step_id] = outcome
                    await self._record_outcome(plan, step, outcome)
                    await self._emit(
                        "step_skipped",
                        plan=plan,
                        step=step,
                        outcome=outcome,
                        reason="dependency failed",
                    )
                    continue
                jobs.append(step)
                await self._emit("step_scheduled", plan=plan, step=step, status="scheduled")
            if not jobs:
                continue
            logger.info(
                "[orchestrator] wave %d/%d: %s",
                wave_index + 1,
                len(waves),
                [s.step_id for s in jobs],
            )
            raw = await asyncio.gather(
                *(
                    self._run_with_retry(plan, step, state, base_ctx, cancel_event, deadline_at)
                    for step in jobs
                ),
                return_exceptions=True,
            )
            for step, outcome in zip(jobs, raw, strict=False):
                if isinstance(outcome, BaseException):
                    outcome = StepOutcome(
                        step_id=step.step_id,
                        worker=step.worker,
                        status="failed",
                        error_type=type(outcome).__name__,
                        error_message=str(outcome)[:2000],
                        reason="unhandled exception",
                    )
                # optional/best_effort 失败按 policy 跳过（不阻断依赖）
                if outcome.status in ("failed", "dead_lettered") and step.policy != "required":
                    outcome = outcome.model_copy(
                        update={"status": "skipped", "reason": "optional failure ignored"}
                    )
                results[step.step_id] = outcome
                await self._record_outcome(plan, step, outcome)
                # 终态事件统一发射（canceled 无对应事件类型，不发）
                if outcome.status in ("succeeded", "failed", "dead_lettered"):
                    await self._emit(outcome.status, plan=plan, step=step, outcome=outcome)
                elif outcome.status == "skipped":
                    await self._emit(
                        "step_skipped",
                        plan=plan,
                        step=step,
                        outcome=outcome,
                        reason="optional failure ignored",
                    )

        if canceled or self._is_canceled(cancel_event, deadline_at):
            canceled = True
            if self.ledger is not None:
                try:
                    await self.ledger.cancel_run(plan.run_id, reason="canceled by user/deadline")
                except Exception:
                    logger.warning("[orchestrator] ledger cancel_run failed")
        status = self._final_status(canceled, results)
        await self._emit("run_canceled" if canceled else "run_finished", plan=plan, status=status)
        duration_ms = int((time.perf_counter() - started) * 1000)
        unscheduled_status = "canceled" if canceled else "skipped"
        step_outcomes = [
            results.get(s.step_id)
            or StepOutcome(
                step_id=s.step_id,
                worker=s.worker,
                status=unscheduled_status,
                reason="not scheduled",
            )
            for s in plan.steps
        ]
        return RunOutcome(
            run_id=run_id,
            plan_id=plan_id,
            status=status,
            waves=len(waves),
            steps=step_outcomes,
            duration_ms=duration_ms,
        )

    # ── 内部 ──────────────────────────────────────────────────

    async def _run_with_retry(
        self,
        plan: PipelinePlan,
        step: PlanStep,
        state: dict[str, Any],
        base_ctx: dict[str, Any],
        cancel_event: asyncio.Event | None,
        deadline_at: float | None,
    ) -> StepOutcome:
        adapter = self.registry.get(step.worker)
        if adapter is None:
            return StepOutcome(
                step_id=step.step_id,
                worker=step.worker,
                status="failed",
                error_type="unregistered_worker",
                reason=f"worker not registered: {step.worker}",
            )
        spec = adapter.spec
        max_attempts = spec.max_attempts or self.default_max_attempts
        attempt = 0
        last_result: WorkerResult | None = None
        last_claim: Any = None

        while attempt < max_attempts:
            if cancel_event is not None and cancel_event.is_set():
                return StepOutcome(
                    step_id=step.step_id, worker=step.worker, status="canceled", attempt=attempt + 1
                )
            if deadline_at is not None and time.monotonic() > deadline_at:
                return StepOutcome(
                    step_id=step.step_id, worker=step.worker, status="canceled", attempt=attempt + 1
                )

            attempt += 1
            if attempt > 1:
                await self._emit(
                    "retrying",
                    plan=plan,
                    step=step,
                    status="retrying",
                    attempt=attempt - 1,
                    version=spec.version,
                )
            claim = None
            if self.ledger is not None:
                try:
                    claim = await self.ledger.begin_attempt(
                        run_id=plan.run_id,
                        step_id=step.step_id,
                        owner_id=self.owner_id,
                        attempt=attempt,
                    )
                except Exception as exc:
                    # 其他恢复者已接管/步骤进入终态：不执行，避免重复业务写入
                    logger.warning(
                        "[orchestrator] ledger claim rejected for %s: %s", step.step_id, exc
                    )
                    return StepOutcome(
                        step_id=step.step_id,
                        worker=step.worker,
                        status="failed",
                        attempt=attempt,
                        error_type="lease_conflict",
                        error_message=str(exc)[:2000],
                        reason="lease_conflict",
                    )
            last_claim = claim
            lease = WorkerLease(
                owner_id=self.owner_id,
                run_id=plan.run_id,
                step_id=step.step_id,
                expires_at=(
                    claim.lease_expires_at
                    if claim is not None
                    else _utc_now() + timedelta(seconds=self.lease_seconds)
                ),
                fencing_token=claim.fencing_token if claim is not None else attempt,
            )
            step_ctx = dict(
                base_ctx,
                step_id=step.step_id,
                worker=step.worker,
                attempt=attempt,
                input_refs=step.input_refs,
            )
            await self._emit(
                "worker_started",
                plan=plan,
                step=step,
                status="running",
                attempt=attempt,
                version=spec.version,
            )
            try:
                async with (
                    self._global_sem,
                    await self._user_sem(base_ctx.get("user_id", "")),
                    await self._provider_sem(spec.concurrency_group),
                    await self._worker_sem(step.worker),
                ):
                    last_result = await asyncio.wait_for(
                        adapter.execute(state, step_ctx, lease),
                        timeout=spec.timeout_s,
                    )
            except (TimeoutError, asyncio.CancelledError):
                # wait_for 超时取消内层任务时，个别版本会以 CancelledError 冒泡，
                # 语义等同超时，统一按可重试超时处理。
                last_result = WorkerResult(
                    step_id=step.step_id,
                    worker=step.worker,
                    idempotency_key="",
                    input_hash="",
                    result_hash="",
                    status="failed",
                    error_type="timeout",
                    error_message=f"worker timeout after {spec.timeout_s}s",
                    retryable=True,
                    attempt=attempt,
                )
            except Exception as exc:
                logger.warning(
                    "[orchestrator] step %s attempt %d crashed: %s", step.step_id, attempt, exc
                )
                last_result = WorkerResult(
                    step_id=step.step_id,
                    worker=step.worker,
                    idempotency_key="",
                    input_hash="",
                    result_hash="",
                    status="failed",
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:2000],
                    retryable=True,
                    attempt=attempt,
                )

            if last_result.status == "succeeded":
                if claim is not None:
                    try:
                        await self.ledger.complete(
                            run_id=plan.run_id,
                            step_id=step.step_id,
                            owner_id=self.owner_id,
                            fencing_token=claim.fencing_token,
                            result=last_result,
                        )
                    except Exception as exc:
                        # 业务写入已成功且幂等；fencing 被接管时仅记录冲突
                        logger.warning(
                            "[orchestrator] ledger complete rejected for %s: %s", step.step_id, exc
                        )
                return StepOutcome(
                    step_id=step.step_id,
                    worker=step.worker,
                    status="succeeded",
                    attempt=attempt,
                    idempotency_key=last_result.idempotency_key,
                    input_hash=last_result.input_hash,
                    result_hash=last_result.result_hash,
                    duration_ms=last_result.duration_ms,
                )
            # 失败：可重试则记 failed 供下一轮 begin_attempt；非重试直接终态
            if claim is not None:
                try:
                    await self.ledger.fail(
                        run_id=plan.run_id,
                        step_id=step.step_id,
                        owner_id=self.owner_id,
                        fencing_token=claim.fencing_token,
                        status="failed",
                        error_type=last_result.error_type,
                        error_message=last_result.error_message,
                        retryable=last_result.retryable,
                        result_hash=last_result.result_hash,
                    )
                except Exception as exc:
                    logger.warning(
                        "[orchestrator] ledger fail rejected for %s: %s", step.step_id, exc
                    )
            if not last_result.retryable:
                break

        assert last_result is not None
        final_status = "dead_lettered" if last_result.retryable else "failed"
        if final_status == "dead_lettered" and last_claim is not None:
            try:
                await self.ledger.mark_dead_lettered(
                    run_id=plan.run_id,
                    step_id=step.step_id,
                    error_type=last_result.error_type,
                    error_message=last_result.error_message,
                    result_hash=last_result.result_hash,
                    retryable=True,
                    recovery_hint=(
                        "retry via POST /api/pipeline/runs/{run_id}/steps/{step_id}/replay"
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "[orchestrator] ledger dead-letter failed for %s: %s", step.step_id, exc
                )
        return StepOutcome(
            step_id=step.step_id,
            worker=step.worker,
            status=final_status,
            attempt=last_result.attempt,
            error_type=last_result.error_type,
            error_message=last_result.error_message,
            idempotency_key=last_result.idempotency_key,
            input_hash=last_result.input_hash,
            result_hash=last_result.result_hash,
            duration_ms=last_result.duration_ms,
            reason=f"attempts exhausted after {last_result.attempt}"
            if final_status == "dead_lettered"
            else last_result.error_type or "failed",
        )

    async def _record_outcome(
        self,
        plan: PipelinePlan,
        step: PlanStep,
        outcome: StepOutcome,
    ) -> None:
        """把 Orchestrator 内部决策同步到 Step Ledger（缺省 ledger 时为纯内存）。"""
        if self.ledger is None:
            return
        try:
            if outcome.status == "skipped":
                # 依赖失败时 ledger 中仍是 pending；optional/best_effort 失败
                # 转跳过时 ledger 中是 failed/dead_lettered —— force_skip 均覆盖。
                updated = await self.ledger.force_skip(
                    run_id=plan.run_id,
                    step_id=step.step_id,
                    reason=outcome.reason or "skipped",
                )
                if updated is None:
                    # 已被并发恢复者置为终态（如 succeeded），不再覆盖
                    logger.info("[orchestrator] ledger skip no-op for %s", step.step_id)
            # succeeded/failed/dead_lettered 已在 _run_with_retry 记账，无需重复
        except Exception as exc:
            logger.warning(
                "[orchestrator] ledger outcome recording failed for %s: %s", step.step_id, exc
            )

    def _worker_version(self, worker: str) -> str:
        """从注册表取 Worker 版本（事件观测用），未注册时返回空串。"""
        adapter = self.registry.get(worker)
        return adapter.spec.version if adapter is not None else ""

    async def _emit(
        self,
        event_type: str,
        *,
        plan: PipelinePlan,
        step: PlanStep | None = None,
        outcome: StepOutcome | None = None,
        status: str = "",
        attempt: int = 0,
        version: str = "",
        reason: str = "",
    ) -> None:
        """发射观测事件（Step 9）；无 emitter 或失败时静默跳过。"""
        if self.emitter is None:
            return
        try:
            await self.emitter.emit(
                event_type=event_type,
                run_id=plan.run_id,
                plan_id=plan.plan_id,
                step_id=step.step_id if step is not None else "",
                worker=step.worker if step is not None else "",
                version=version or self._worker_version(step.worker if step is not None else ""),
                attempt=attempt if attempt else (outcome.attempt if outcome is not None else 0),
                input_hash=outcome.input_hash if outcome is not None else "",
                result_hash=outcome.result_hash if outcome is not None else "",
                duration_ms=outcome.duration_ms if outcome is not None else 0,
                error_type=outcome.error_type if outcome is not None else None,
                status=status or (outcome.status if outcome is not None else ""),
            )
        except Exception:
            logger.warning(
                "[orchestrator] emit %s failed (run=%s step=%s)",
                event_type,
                plan.run_id,
                step.step_id if step is not None else "",
            )

    def _deps_ok(
        self,
        plan: PipelinePlan,
        step: PlanStep,
        results: dict[str, StepOutcome],
    ) -> bool:
        by_id = {s.step_id: s for s in plan.steps}
        for dep in step.depends_on:
            outcome = results.get(dep)
            if outcome is None:
                return False
            if outcome.status == "succeeded":
                continue
            if outcome.status == "skipped" and by_id[dep].policy != "required":
                continue
            return False
        return True

    @staticmethod
    def _is_canceled(cancel_event: asyncio.Event | None, deadline_at: float | None) -> bool:
        return (cancel_event is not None and cancel_event.is_set()) or (
            deadline_at is not None and time.monotonic() > deadline_at
        )

    @staticmethod
    def _final_status(canceled: bool, results: dict[str, StepOutcome]) -> str:
        if canceled:
            return "canceled"
        if any(r.status == "failed" for r in results.values()):
            return "failed"
        # 死信（重试耗尽）步骤保留已确认产物，可单独重放 → partial 而非 failed
        if any(r.status == "dead_lettered" for r in results.values()):
            return "partial"
        return "completed"

    # ── 并发配额 ──────────────────────────────────────────────

    async def _user_sem(self, user_id: str) -> asyncio.Semaphore:
        async with self._sem_lock:
            if user_id not in self._user_sems:
                self._user_sems[user_id] = asyncio.Semaphore(self.user_concurrency)
            return self._user_sems[user_id]

    async def _provider_sem(self, group: str) -> asyncio.Semaphore:
        async with self._sem_lock:
            if group not in self._provider_sems:
                self._provider_sems[group] = asyncio.Semaphore(
                    self.provider_concurrency.get(group, 2)
                )
            return self._provider_sems[group]

    async def _worker_sem(self, worker: str) -> asyncio.Semaphore:
        async with self._sem_lock:
            if worker not in self._worker_sems:
                self._worker_sems[worker] = asyncio.Semaphore(self.worker_concurrency)
            return self._worker_sems[worker]
