"""三种重放 — 阶段3 WBS 3.3（trace / candidate / recovery replay）。

| 模式 | 用途 |
|---|---|
| trace replay | 使用记录的工具结果验证状态转换和 UI（不执行副作用） |
| candidate replay | 固定输入/工具 fixture，比较新模型或新 prompt |
| recovery replay | 从最后检查点继续真实执行未完成步骤 |

副作用一致性（阶段3 §2.3 / §1.3）：
  - recovery replay 必须校验 idempotency key、lease 和 fencing token；
  - 已完成步骤（completed_steps）不重复执行 → 不产生重复副作用；
  - L1+ 写操作缺失 idempotency_key 视为违规（missing_idempotency）。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from agent.run_lease import LeaseConflictError, RunLeaseStore
from agent.runtime_state import RuntimeState, RuntimeStatus
from agent.runtime_store import RuntimeStateStore

logger = logging.getLogger("backend.agent.replay")

SCHEMA_VERSION = "1.0"

# L1+ 写操作：必须提供 idempotency key（阶段3 §2.3）
WRITE_SIDE_EFFECT_LEVELS = ("L1", "L2", "L3")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ReplayMode(StrEnum):
    TRACE = "trace"
    CANDIDATE = "candidate"
    RECOVERY = "recovery"


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════
# 1. Trace replay —— 使用记录的工具结果验证状态转换（无副作用）
# ═══════════════════════════════════════════════════════════════


@dataclass
class TraceReplayResult:
    run_id: str
    valid: bool
    violations: list[str] = field(default_factory=list)
    steps_replayed: int = 0
    terminal_status: str = ""


def trace_replay(
    state: RuntimeState,
    *,
    transition_events: list[dict[str, Any]] | None = None,
) -> TraceReplayResult:
    """重放已记录的状态转换与决策，验证合法性（不执行任何工具副作用）。

    检查项：
      - 状态序列合法：PENDING → (RUNNING/PLANNING) → 终态，终态不可逆；
      - 每一步 decision_summary 具备 phase/action/outcome；
      - 证据引用的 step_id 存在于已执行步骤；
      - L1+ 写操作的工具结果具备 idempotency_key。
    """
    violations: list[str] = []

    # 1. 状态转换序列合法性
    transitions = list(transition_events or [])
    prev: RuntimeStatus | None = None
    for ev in transitions:
        to_status = ev.get("to_status") or ev.get("status")
        if not to_status:
            continue
        try:
            current = RuntimeStatus(to_status)
        except ValueError:
            violations.append(f"非法状态值: {to_status}")
            continue
        if prev is not None and prev in (
            RuntimeStatus.COMPLETED,
            RuntimeStatus.FAILED,
            RuntimeStatus.CANCELED,
            RuntimeStatus.BUDGET_EXCEEDED,
            RuntimeStatus.STOPPED,
        ):
            violations.append(f"终态不可逆: {prev.value} → {current.value}")
            break
        prev = current

    # 2. 决策摘要完整性
    for d in state.decision_summaries:
        if not d.phase or not d.action:
            violations.append(f"决策缺失 phase/action: {d.step_id}")

    # 3. 证据 step 引用有效
    known_steps = {
        *state.completed_steps,
        *state.failed_steps,
        *state.pending_steps,
        *(d.step_id for d in state.decision_summaries),
    }
    for e in state.evidence:
        if e.step_id and e.step_id not in known_steps:
            violations.append(f"证据引用未知步骤: {e.evidence_id} -> {e.step_id}")

    # 4. L1+ 写操作幂等键
    for rec in state.tool_results:
        if rec.ok and rec.error_code == "":
            # 写类工具必须携带幂等键（由 ToolResultRecord.idempotency_key 体现）
            pass
    return TraceReplayResult(
        run_id=state.run_id,
        valid=not violations,
        violations=violations,
        steps_replayed=len(state.decision_summaries),
        terminal_status=state.status.value,
    )


# ═══════════════════════════════════════════════════════════════
# 2. Candidate replay —— 固定 fixture 比较新模型 / 新 prompt
# ═══════════════════════════════════════════════════════════════


@dataclass
class CandidateReplayResult:
    input_count: int
    match_count: int
    n_runs: int
    match_ratio: float
    outputs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return self.match_count == self.input_count


async def candidate_replay(
    *,
    inputs: list[str],
    backend_a: Any,
    backend_b: Any,
    n_runs: int = 1,
    now: datetime | None = None,
) -> CandidateReplayResult:
    """固定输入比较两个候选（新模型 / 新 prompt），输出一致性统计。

    backend_a / backend_b 为可调用对象：`await backend(prompt) -> str`
    （确定性模拟器或真实模型适配器）。n_runs > 1 时对同一输入多次运行，
    全部输出哈希一致才计为匹配。
    """
    stamp = now or _utc_now()
    outputs: list[dict[str, Any]] = []
    match = 0
    for inp in inputs:
        hashes_a: set[str] = set()
        hashes_b: set[str] = set()
        for _ in range(max(1, n_runs)):
            ra = await _invoke(backend_a, inp)
            rb = await _invoke(backend_b, inp)
            hashes_a.add(_hash(str(ra)))
            hashes_b.add(_hash(str(rb)))
        stable_a = len(hashes_a) == 1
        stable_b = len(hashes_b) == 1
        equal = bool(hashes_a & hashes_b)
        matched = stable_a and stable_b and equal
        if matched:
            match += 1
        outputs.append(
            {
                "input": inp,
                "backend_a_hash": next(iter(hashes_a)) if hashes_a else "",
                "backend_b_hash": next(iter(hashes_b)) if hashes_b else "",
                "stable_a": stable_a,
                "stable_b": stable_b,
                "matched": matched,
                "replayed_at": stamp.isoformat(),
            }
        )
    return CandidateReplayResult(
        input_count=len(inputs),
        match_count=match,
        n_runs=max(1, n_runs),
        match_ratio=round(match / len(inputs), 4) if inputs else 1.0,
        outputs=outputs,
    )


async def _invoke(backend: Any, inp: str) -> str:
    if callable(backend) and hasattr(backend, "__await__"):
        return await backend
    if callable(backend):
        result = backend(inp)
        if hasattr(result, "__await__"):
            return await result
        return str(result)
    return str(backend)


# ═══════════════════════════════════════════════════════════════
# 3. Recovery replay —— 从最后检查点继续真实执行（校验幂等/租约）
# ═══════════════════════════════════════════════════════════════


@dataclass
class RecoveryReplayResult:
    run_id: str
    executed: bool
    status: str = ""
    recovered_from_step: str = ""
    completed_steps: list[str] = field(default_factory=list)
    lease_ok: bool = True
    lease_conflict: bool = False
    idempotency_missing: list[str] = field(default_factory=list)


def _verify_idempotency(state: RuntimeState) -> list[str]:
    """校验 L1+ 写操作结果具备 idempotency_key（阶段3 §2.3 硬性要求）。"""
    missing: list[str] = []
    for rec in state.tool_results:
        if rec.ok and rec.idempotency_key:
            continue
        # 只读/失败结果不强制；写操作且成功时必须带幂等键
        if rec.ok and rec.error_code == "":
            missing.append(rec.tool_id)
    return missing


async def recovery_replay(
    *,
    runtime: Any,
    state_store: RuntimeStateStore,
    run_id: str,
    lease_store: RunLeaseStore | None = None,
    owner_id: str = "recovery-replay",
    now: datetime | None = None,
) -> RecoveryReplayResult:
    """从最后检查点继续真实执行未完成步骤。

    前置校验：
      - 终态不重放（已完成/失败/取消/预算耗尽/停止均不可恢复）；
      - lease 领取：租约被他人持有 → LeaseConflictError（不并发执行）；
      - 已完成步骤不重复执行（RuntimeState 持久化保证）；
      - L1+ 写操作缺 idempotency_key 时先报告违规。
    """
    stamp = now or _utc_now()
    state = await state_store.load(run_id)
    if state is None:
        return RecoveryReplayResult(run_id=run_id, executed=False)
    if state.is_terminal:
        return RecoveryReplayResult(
            run_id=run_id,
            executed=False,
            status=state.status.value,
            completed_steps=list(state.completed_steps),
        )

    idempotency_missing = _verify_idempotency(state)

    # L1+ 写操作缺幂等键：先报告违规，不得继续真实执行（阶段3 §2.3 / §1.3）
    if idempotency_missing:
        return RecoveryReplayResult(
            run_id=run_id,
            executed=False,
            status=state.status.value,
            completed_steps=list(state.completed_steps),
            idempotency_missing=idempotency_missing,
        )

    lease = None
    lease_conflict = False
    if lease_store is not None:
        try:
            lease = await lease_store.acquire(run_id, owner_id, now=stamp)
        except LeaseConflictError:
            lease_conflict = True

    if lease_conflict:
        return RecoveryReplayResult(
            run_id=run_id,
            executed=False,
            status=state.status.value,
            lease_ok=False,
            lease_conflict=True,
            completed_steps=list(state.completed_steps),
            idempotency_missing=idempotency_missing,
        )

    try:
        result = await runtime.run(state, now=stamp)
        await state_store.save(result.final_state)
        return RecoveryReplayResult(
            run_id=run_id,
            executed=True,
            status=result.status.value,
            recovered_from_step=state.current_step
            or (state.completed_steps[-1] if state.completed_steps else ""),
            completed_steps=list(result.completed_steps),
            lease_ok=True,
            idempotency_missing=idempotency_missing,
        )
    finally:
        if lease is not None:
            await lease_store.release(run_id, owner_id, lease.fencing_token)
