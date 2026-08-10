"""ExecutionStepLedger — 阶段三 Step 6。

业务步骤账本：只负责业务幂等、租约/fencing、查询、死信和人工重放；
LangGraph 图状态恢复仍由 checkpointer.py/MongoDBSaver 承担（职责分离）。

文档模型（execution_step_ledger）：
    run_id, plan_id, step_id, worker, status, attempt,
    idempotency_key, input_hash, result_hash,
    lease_owner, lease_expires_at, fencing_token,
    error_type, retryable, started_at, finished_at, expires_at

设计约束：
  - (run_id, step_id) 唯一，idempotency_key 唯一；
  - 状态迁移全部 compare-and-set：接管后旧 Worker 的迟到写入
    因 lease_owner/fencing_token 不匹配被拒绝；
  - 恢复流程：加载 plan/input snapshot → 读取 graph checkpoint →
    验证 succeeded step 业务产物/hash → 跳过成功步骤 → 接管过期 running →
    仅执行 pending/retryable；
  - reconciliation job：发现 checkpoint/ledger 不一致写入修复队列，
    不盲目重跑。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel, ReturnDocument

from agent.plan_contracts import PipelinePlan, WorkerName
from agent.worker_registry import WorkerResult

logger = logging.getLogger("backend.agent.execution_step_ledger")

COLLECTION = "execution_step_ledger"
REPAIR_QUEUE_COLLECTION = "ledger_repair_queue"

StepStatus = Literal[
    "pending", "running", "succeeded", "failed", "skipped", "dead_lettered", "canceled"
]

_RUNNABLE_STATUSES: tuple[str, ...] = ("pending", "failed", "dead_lettered")


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ═══════════════════════════════════════════════════════════════
# 模型
# ═══════════════════════════════════════════════════════════════


class StepLedgerEntry(BaseModel):
    """execution_step_ledger 单条账本记录。"""

    run_id: str = Field(..., min_length=1, max_length=100)
    plan_id: str = Field(..., min_length=1, max_length=100)
    step_id: str = Field(..., min_length=1, max_length=64)
    worker: WorkerName
    status: StepStatus = "pending"
    attempt: int = Field(default=1, ge=1)
    idempotency_key: str = Field(default="", max_length=500)
    input_hash: str = Field(default="", max_length=100)
    result_hash: str = Field(default="", max_length=100)
    lease_owner: str = Field(default="", max_length=100)
    lease_expires_at: datetime | None = None
    fencing_token: int = Field(default=0, ge=0)
    error_type: str | None = Field(default=None, max_length=100)
    retryable: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    # TTL/归档：默认 90 天，与 planner_plans 保持一致
    expires_at: datetime = Field(default_factory=lambda: _utc_now() + timedelta(days=90))

    @property
    def lease_expired(self) -> bool:
        return self.lease_expires_at is not None and self.lease_expires_at <= _utc_now()


class RecoveryResult(BaseModel):
    """恢复流程输出：仅返回需要执行的步骤。"""

    run_id: str = Field(..., min_length=1, max_length=100)
    ok: bool = True
    plan: PipelinePlan | None = None
    to_execute: list[StepLedgerEntry] = Field(default_factory=list)
    skipped: list[StepLedgerEntry] = Field(default_factory=list)
    taken_over: list[StepLedgerEntry] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class ReconciliationIssue(BaseModel):
    """checkpoint/ledger 不一致记录（只记录，不盲目重跑）。"""

    run_id: str = Field(..., min_length=1, max_length=100)
    step_id: str = Field(..., min_length=0, max_length=64)
    issue_type: str = Field(..., min_length=1, max_length=64)
    severity: Literal["auto_repair", "manual_review"]
    detail: str = Field(default="", max_length=500)


class ReconciliationResult(BaseModel):
    """reconciliation job 输出：issues + 已入修复队列数量。"""

    run_id: str = Field(..., min_length=1, max_length=100)
    issues: list[ReconciliationIssue] = Field(default_factory=list)
    repair_enqueued: int = Field(default=0, ge=0)


class LeaseConflictError(Exception):
    """租约/fencing 不匹配或步骤已进入终态，写入被拒绝。"""


# ═══════════════════════════════════════════════════════════════
# ExecutionStepLedger
# ═══════════════════════════════════════════════════════════════


class ExecutionStepLedger:
    """execution_step_ledger 仓储：CAS 状态迁移 + 恢复 + 对账。

    ``db`` 暴露 Motor 风格的异步集合（find/find_one/insert_one/
    find_one_and_update/update_many/create_indexes）。恢复与对账所需
    的 plan/checkpoint 通过回调注入，本模块不直接耦合 LangGraph。
    """

    def __init__(
        self,
        db: Any,
        *,
        collection: str = COLLECTION,
        repair_queue_collection: str = REPAIR_QUEUE_COLLECTION,
        lease_seconds: int = 120,
        expires_days: int = 90,
        stale_running_grace_seconds: int = 3600,
    ):
        self.db = db
        self.col = db[collection]
        self.repair_col = db[repair_queue_collection]
        self.collection_name = collection
        self.repair_queue_collection = repair_queue_collection
        self.lease_seconds = max(1, lease_seconds)
        self.expires_days = max(1, expires_days)
        self.stale_running_grace_seconds = max(1, stale_running_grace_seconds)

    # ── 索引 ──────────────────────────────────────────────────

    def index_specs(self) -> dict[str, list[IndexModel]]:
        """幂等索引清单（与 db/mongo.py ensure_indexes 保持一致）。"""
        return {
            self.collection_name: [
                # (run_id, step_id) 唯一
                IndexModel(
                    [("run_id", ASCENDING), ("step_id", ASCENDING)],
                    unique=True,
                    name="uq_step_ledger_run_step",
                ),
                # idempotency_key 唯一（失败/跳过为空串，sparse 允许空串并存）
                IndexModel(
                    [("idempotency_key", ASCENDING)],
                    unique=True,
                    sparse=True,
                    name="uq_step_ledger_idempotency",
                ),
                # 过期 running 接管查询
                IndexModel(
                    [("status", ASCENDING), ("lease_expires_at", ASCENDING)],
                    name="idx_step_ledger_status_lease",
                ),
                # 按 plan 检索
                IndexModel(
                    [("plan_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_step_ledger_plan_created",
                ),
                # dead-letter 查询
                IndexModel(
                    [("status", ASCENDING), ("run_id", ASCENDING)],
                    name="idx_step_ledger_deadletter",
                ),
                # TTL/归档
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="ttl_step_ledger_expires",
                ),
            ],
            self.repair_queue_collection: [
                IndexModel(
                    [("run_id", ASCENDING), ("step_id", ASCENDING)],
                    name="idx_ledger_repair_run_step",
                ),
                IndexModel(
                    [("status", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_ledger_repair_status_created",
                ),
            ],
        }

    async def ensure_indexes(self) -> list[str]:
        """幂等创建账本与修复队列索引。"""
        created: list[str] = []
        for name, indexes in self.index_specs().items():
            created.extend(await self.db[name].create_indexes(indexes))
        return created

    # ── 初始化 ────────────────────────────────────────────────

    async def init_run(
        self,
        plan: PipelinePlan,
        *,
        expires_at: datetime | None = None,
    ) -> list[StepLedgerEntry]:
        """为计划全部步骤建立 pending 账本（幂等：已存在则跳过）。"""
        now = _utc_now()
        exp = expires_at or (now + timedelta(days=self.expires_days))
        entries: list[StepLedgerEntry] = []
        for step in plan.steps:
            if await self.col.find_one({"run_id": plan.run_id, "step_id": step.step_id}):
                continue
            entry = StepLedgerEntry(
                run_id=plan.run_id,
                plan_id=plan.plan_id,
                step_id=step.step_id,
                worker=step.worker,
                status="pending",
                expires_at=exp,
            )
            await self.col.insert_one(entry.model_dump())
            entries.append(entry)
        return entries

    # ── 查询 ──────────────────────────────────────────────────

    async def get_step(self, run_id: str, step_id: str) -> StepLedgerEntry | None:
        doc = await self.col.find_one({"run_id": run_id, "step_id": step_id})
        return StepLedgerEntry.model_validate(doc) if doc else None

    async def list_run_steps(self, run_id: str) -> list[StepLedgerEntry]:
        docs = await self._list_docs({"run_id": run_id})
        return [StepLedgerEntry.model_validate(d) for d in docs]

    # ── CAS 状态迁移 ──────────────────────────────────────────

    async def begin_attempt(
        self,
        *,
        run_id: str,
        step_id: str,
        owner_id: str,
        attempt: int,
        idempotency_key: str = "",
        input_hash: str = "",
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> StepLedgerEntry:
        """CAS 领取步骤：仅 pending/failed/dead_lettered 可进入 running。

        已 succeeded/skipped/canceled 的步骤无法被再次领取（返回终态）；
        失败/死信步骤可被人工重放再次领取（Step 7 人工重放语义）。
        """
        now = now or _utc_now()
        expires = now + timedelta(seconds=lease_seconds or self.lease_seconds)
        doc = await self.col.find_one_and_update(
            {
                "run_id": run_id,
                "step_id": step_id,
                "status": {"$in": list(_RUNNABLE_STATUSES)},
            },
            {
                "$set": {
                    "status": "running",
                    "attempt": attempt,
                    "idempotency_key": idempotency_key,
                    "input_hash": input_hash,
                    "lease_owner": owner_id,
                    "lease_expires_at": expires,
                    "fencing_token": attempt,
                    "started_at": now,
                    "updated_at": now,
                    "error_type": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise LeaseConflictError(
                f"step not claimable: run={run_id} step={step_id} status terminal or held"
            )
        return StepLedgerEntry.model_validate(doc)

    async def complete(
        self,
        *,
        run_id: str,
        step_id: str,
        owner_id: str,
        fencing_token: int,
        result: WorkerResult,
        now: datetime | None = None,
    ) -> StepLedgerEntry:
        """CAS 成功：仅当前 owner + 当前 fencing_token 可提交。"""
        now = now or _utc_now()
        doc = await self.col.find_one_and_update(
            {
                "run_id": run_id,
                "step_id": step_id,
                "status": "running",
                "lease_owner": owner_id,
                "fencing_token": fencing_token,
            },
            {
                "$set": {
                    "status": "succeeded",
                    "idempotency_key": result.idempotency_key,
                    "input_hash": result.input_hash,
                    "result_hash": result.result_hash,
                    "lease_owner": "",
                    "lease_expires_at": None,
                    "finished_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise LeaseConflictError(
                f"stale write rejected: run={run_id} step={step_id} (lease/fencing mismatch)"
            )
        return StepLedgerEntry.model_validate(doc)

    async def fail(
        self,
        *,
        run_id: str,
        step_id: str,
        owner_id: str,
        fencing_token: int,
        status: Literal["failed", "dead_lettered"],
        error_type: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
        result_hash: str = "",
        now: datetime | None = None,
    ) -> StepLedgerEntry:
        """CAS 失败：仅当前 owner + 当前 fencing_token 可提交。"""
        now = now or _utc_now()
        doc = await self.col.find_one_and_update(
            {
                "run_id": run_id,
                "step_id": step_id,
                "status": "running",
                "lease_owner": owner_id,
                "fencing_token": fencing_token,
            },
            {
                "$set": {
                    "status": status,
                    "result_hash": result_hash,
                    "error_type": error_type,
                    "retryable": retryable,
                    "lease_owner": "",
                    "lease_expires_at": None,
                    "finished_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise LeaseConflictError(
                f"stale write rejected: run={run_id} step={step_id} (lease/fencing mismatch)"
            )
        return StepLedgerEntry.model_validate(doc)

    async def skip(
        self,
        *,
        run_id: str,
        step_id: str,
        reason: str = "",
        now: datetime | None = None,
    ) -> StepLedgerEntry:
        """标记跳过（optional 失败继续 / 依赖失败）。"""
        now = now or _utc_now()
        doc = await self.col.find_one_and_update(
            {
                "run_id": run_id,
                "step_id": step_id,
                "status": {"$in": ["pending", "running"]},
            },
            {
                "$set": {
                    "status": "skipped",
                    "error_type": reason or None,
                    "finished_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise LeaseConflictError(
                f"step not skippable: run={run_id} step={step_id}"
            )
        return StepLedgerEntry.model_validate(doc)

    async def force_skip(
        self,
        *,
        run_id: str,
        step_id: str,
        reason: str = "",
        now: datetime | None = None,
    ) -> StepLedgerEntry | None:
        """终态强制跳过：failed/dead_lettered → skipped（optional 策略继续）。

        orchestrator 在 optional/best_effort 步骤失败被策略跳过时调用；
        跳过发生在失败记账之后，因此必须允许从失败/死信终态迁移。
        """
        now = now or _utc_now()
        doc = await self.col.find_one_and_update(
            {
                "run_id": run_id,
                "step_id": step_id,
                "status": {"$in": ["pending", "running", "failed", "dead_lettered"]},
            },
            {
                "$set": {
                    "status": "skipped",
                    "error_type": reason or None,
                    "finished_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return StepLedgerEntry.model_validate(doc) if doc else None

    async def mark_dead_lettered(
        self,
        *,
        run_id: str,
        step_id: str,
        error_type: str | None = None,
        error_message: str | None = None,
        result_hash: str = "",
        retryable: bool = True,
        now: datetime | None = None,
    ) -> StepLedgerEntry | None:
        """死信升级：running/failed → dead_lettered（重试耗尽）。

        orchestrator 中 retryable 失败在每轮尝试后记 failed（便于下一轮
        begin_attempt 重新领取），耗尽后升级为 dead_lettered。
        """
        now = now or _utc_now()
        doc = await self.col.find_one_and_update(
            {
                "run_id": run_id,
                "step_id": step_id,
                "status": {"$in": ["running", "failed"]},
            },
            {
                "$set": {
                    "status": "dead_lettered",
                    "result_hash": result_hash,
                    "error_type": error_type,
                    "retryable": retryable,
                    "error_message": error_message,
                    "lease_owner": "",
                    "lease_expires_at": None,
                    "finished_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return StepLedgerEntry.model_validate(doc) if doc else None

    async def cancel_run(self, run_id: str, *, reason: str = "") -> int:
        """幂等取消：把 pending/running 步骤置为 canceled。返回受影响数。"""
        now = _utc_now()
        res = await self.col.update_many(
            {"run_id": run_id, "status": {"$in": ["pending", "running"]}},
            {
                "$set": {
                    "status": "canceled",
                    "error_type": reason or None,
                    "finished_at": now,
                    "updated_at": now,
                }
            },
        )
        return int(getattr(res, "modified_count", 0))

    # ── 租约接管 ──────────────────────────────────────────────

    async def takeover_expired(
        self,
        *,
        run_id: str,
        owner_id: str,
        now: datetime | None = None,
    ) -> list[StepLedgerEntry]:
        """接管过期 running：逐个 CAS，防止两个恢复者同时接管。"""
        now = now or _utc_now()
        docs = await self._list_docs(
            {"run_id": run_id, "status": "running", "lease_expires_at": {"$lte": now}}
        )
        taken: list[StepLedgerEntry] = []
        for doc in docs:
            try:
                taken.append(
                    await self._takeover_one(
                        run_id, doc["step_id"], owner_id, int(doc["fencing_token"]) + 1, now
                    )
                )
            except LeaseConflictError as exc:
                logger.info("[ledger] takeover rejected: %s", exc)
        return taken

    async def _takeover_one(
        self,
        run_id: str,
        step_id: str,
        owner_id: str,
        next_token: int,
        now: datetime,
    ) -> StepLedgerEntry:
        """CAS 接管单步：filter 仍校验租约已过期，避免重复接管。"""
        doc = await self.col.find_one_and_update(
            {
                "run_id": run_id,
                "step_id": step_id,
                "status": "running",
                "lease_expires_at": {"$lte": now},
            },
            {
                "$set": {
                    "lease_owner": owner_id,
                    "lease_expires_at": now + timedelta(seconds=self.lease_seconds),
                    "fencing_token": next_token,
                    "attempt": next_token,
                    "started_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise LeaseConflictError(f"takeover rejected: run={run_id} step={step_id}")
        return StepLedgerEntry.model_validate(doc)

    # ── 恢复流程 ──────────────────────────────────────────────

    async def recover_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        load_plan: Callable[[str], Awaitable[PipelinePlan | None]],
        read_checkpoint: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        verify_artifact: Callable[[StepLedgerEntry], Awaitable[bool]] | None = None,
        now: datetime | None = None,
    ) -> RecoveryResult:
        """恢复流程（见模块 docstring 第 6 步语义）。

        - load_plan 失败 → ok=False（不盲目执行）；
        - succeeded 步骤必须通过 verify_artifact 校验才跳过；
        - 过期 running 被接管（fencing_token 递增），旧 Worker 迟到写被拒；
        - 仅返回 pending / failed / dead_lettered / 被接管的步骤。
        """
        now = now or _utc_now()
        plan = await load_plan(run_id)
        if plan is None:
            return RecoveryResult(run_id=run_id, ok=False, issues=["plan/input snapshot not found"])

        entries = await self.list_run_steps(run_id)
        if not entries:
            await self.init_run(plan)
            entries = await self.list_run_steps(run_id)

        to_execute: list[StepLedgerEntry] = []
        skipped: list[StepLedgerEntry] = []
        taken_over: list[StepLedgerEntry] = []
        issues: list[str] = []

        for entry in entries:
            if entry.status == "succeeded":
                verified = True
                if verify_artifact is not None:
                    verified = await verify_artifact(entry)
                if verified:
                    skipped.append(entry)
                else:
                    issues.append(f"succeeded artifact mismatch: {entry.step_id}")
                    await self._enqueue_repair(
                        run_id,
                        entry.step_id,
                        "artifact_mismatch",
                        "manual_review",
                        f"result_hash={entry.result_hash}",
                    )
            elif (
                entry.status == "running"
                and entry.lease_expires_at is not None
                and entry.lease_expires_at <= now
            ):
                try:
                    taken = await self._takeover_one(
                        run_id, entry.step_id, owner_id, entry.fencing_token + 1, now
                    )
                    taken_over.append(taken)
                    to_execute.append(taken)
                except LeaseConflictError as exc:
                    issues.append(str(exc))
            elif entry.status in _RUNNABLE_STATUSES:
                to_execute.append(entry)
            # skipped / canceled 不执行

        return RecoveryResult(
            run_id=run_id,
            plan=plan,
            to_execute=to_execute,
            skipped=skipped,
            taken_over=taken_over,
            issues=issues,
        )

    # ── Reconciliation ────────────────────────────────────────

    async def reconcile(
        self,
        *,
        run_id: str,
        load_plan: Callable[[str], Awaitable[PipelinePlan | None]] | None = None,
        read_checkpoint: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        verify_artifact: Callable[[StepLedgerEntry], Awaitable[bool]] | None = None,
        now: datetime | None = None,
    ) -> ReconciliationResult:
        """对账：发现 checkpoint/ledger 不一致，写入修复队列，不盲目重跑。"""
        now = now or _utc_now()
        issues: list[ReconciliationIssue] = []

        plan = await load_plan(run_id) if load_plan else None
        if plan is None:
            issues.append(
                ReconciliationIssue(
                    run_id=run_id,
                    step_id="",
                    issue_type="plan_missing",
                    severity="manual_review",
                    detail="plan/input snapshot not found",
                )
            )

        entries = await self.list_run_steps(run_id)
        ledger_ids = {e.step_id for e in entries}
        checkpoint_ids: set[str] = set()

        if read_checkpoint:
            cp = await read_checkpoint(run_id)
            if cp is not None:
                checkpoint_ids = set(cp.get("completed_steps") or [])
                for sid in sorted(checkpoint_ids - ledger_ids):
                    issues.append(
                        ReconciliationIssue(
                            run_id=run_id,
                            step_id=sid,
                            issue_type="missing_ledger",
                            severity="auto_repair",
                            detail="checkpoint has step but ledger entry missing",
                        )
                    )

        for entry in entries:
            if entry.status == "succeeded" and checkpoint_ids and entry.step_id not in checkpoint_ids:
                verified = True
                if verify_artifact is not None:
                    verified = await verify_artifact(entry)
                if not verified:
                    issues.append(
                        ReconciliationIssue(
                            run_id=run_id,
                            step_id=entry.step_id,
                            issue_type="succeeded_without_checkpoint",
                            severity="manual_review",
                            detail="ledger succeeded but checkpoint missing and artifact unverifiable",
                        )
                    )
            if entry.status == "running":
                started = entry.started_at or entry.created_at
                if now - started > timedelta(seconds=self.stale_running_grace_seconds):
                    issues.append(
                        ReconciliationIssue(
                            run_id=run_id,
                            step_id=entry.step_id,
                            issue_type="running_stale",
                            severity="manual_review",
                            detail="running step stuck beyond grace period",
                        )
                    )

        for issue in issues:
            await self._enqueue_repair(
                run_id, issue.step_id, issue.issue_type, issue.severity, issue.detail
            )
        return ReconciliationResult(
            run_id=run_id,
            issues=issues,
            repair_enqueued=len(issues),
        )

    # ── 内部 ──────────────────────────────────────────────────

    async def _enqueue_repair(
        self,
        run_id: str,
        step_id: str,
        issue_type: str,
        severity: str,
        detail: str = "",
    ) -> None:
        try:
            await self.repair_col.insert_one(
                {
                    "run_id": run_id,
                    "step_id": step_id,
                    "issue_type": issue_type,
                    "severity": severity,
                    "detail": detail,
                    "status": "open",
                    "created_at": _utc_now(),
                }
            )
        except Exception:
            logger.exception("[ledger] enqueue repair failed")

    async def _list_docs(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        """兼容 Motor cursor（to_list）与内存 fake（async iterable）。"""
        cursor = self.col.find(query)
        if hasattr(cursor, "to_list"):
            return await cursor.to_list(length=None)
        return [doc async for doc in cursor]
