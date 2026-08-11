"""BudgetManager — 阶段1 Token/成本硬预算（WBS 1.3）。

预算采用"预留—结算—释放"机制（对齐 00-统一架构 第 3 节）：
  1. 调用前按最坏或可配置上界预留 Token/成本；预算不足时不发起调用；
  2. 调用后使用 provider usage 和实际工具结果结算；
  3. 预留未使用部分释放；
  4. 超额或 usage 缺失必须记录原因并进入保守估算；
  5. finalization 同样需要预算，不允许成为预算旁路。

预算分层（2.1）：
  - REQUEST：单次 run Token、费用、时延、步骤、工具和重试
  - USER：日/月 Token 与费用配额、并发上限
  - TENANT：总成本、峰值并发、模型允许列表
  - MODEL：单调用上下文、输出、RPS、TPM
  - TOOL：结果大小、调用次数、超时和外部费用

预算水位（2.2）：
  - 70%：发出预算预警，禁止低价值扩展检索；
  - 90%：启用压缩、小模型或缩短输出；
  - 100%：停止新工具和新规划，只允许有预留额度的 finalization；
  - 无 finalization 预留：返回结构化 budget_exhausted，不得额外调用模型。

安全约束：不保存 prompt、工具参数/结果原文、密钥或私有推理。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger("backend.agent.budget_manager")

# 预留给 finalization 的 token 下限（不足则视为无 finalization 预算）
MIN_FINALIZATION_RESERVE_TOKENS = 200


# ═══════════════════════════════════════════════════════════════
# 预算计划（不可变）
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class BudgetPlan:
    """单次 run 的预算上限（对齐统一预算模型字段）。"""

    max_input_tokens: int = 24000
    max_output_tokens: int = 4000
    max_total_tokens: int = 0  # 0 = 由单项上限兜底
    max_tool_result_tokens: int = 3000  # 单条工具结果 token 上限
    max_cost_usd: float = 0.0  # 0 = 不限制
    max_steps: int = 5
    max_tool_calls: int = 8
    max_retries: int = 2
    max_runtime_seconds: int = 30
    max_parallel_tools: int = 3
    per_model_call_limit: int = 0  # 0 = 不限制
    per_tool_call_limit: int = 0  # 0 = 不限制
    warning_ratio: float = 0.7  # 水位预警
    compress_ratio: float = 0.9  # 水位压缩
    finalization_reserve_tokens: int = MIN_FINALIZATION_RESERVE_TOKENS
    deadline_at: datetime | None = None


# ═══════════════════════════════════════════════════════════════
# 分层配额
# ═══════════════════════════════════════════════════════════════


class BudgetTier(StrEnum):
    """预算分层。"""

    REQUEST = "request"
    USER = "user"
    TENANT = "tenant"
    MODEL = "model"
    TOOL = "tool"


@dataclass(frozen=True)
class TierQuota:
    """某一层的配额上限。"""

    tier: BudgetTier
    key: str  # 层内标识：如 user_id / tenant_id / model_id / tool_name
    max_total_tokens: int = 0  # 0 = 不限制
    max_cost_usd: float = 0.0  # 0 = 不限制
    max_concurrency: int = 0  # 0 = 不限制


class BudgetStatus(StrEnum):
    """预算水位状态。"""

    OK = "ok"
    WARNING = "warning"  # >=70%
    COMPRESS = "compress"  # >=90%
    EXHAUSTED = "exhausted"  # 100%


class BudgetExhaustedError(Exception):
    """预算耗尽：不允许发起新调用。"""


class NoFinalizationBudgetError(BudgetExhaustedError):
    """无 finalization 预留预算：返回结构化 budget_exhausted。"""


# ═══════════════════════════════════════════════════════════════
# 预留与结算记录
# ═══════════════════════════════════════════════════════════════


class ReservationKind(StrEnum):
    """预留类型。"""

    LLM = "llm"
    TOOL = "tool"
    FINALIZATION = "finalization"


class ReservationStatus(StrEnum):
    """预留状态。"""

    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"


@dataclass
class BudgetReservation:
    """一次预算预留记录（可变，结算/释放后更新）。"""

    reservation_id: str
    kind: ReservationKind
    scope: str  # request / user:xxx / tenant:xxx / model:xxx / tool:xxx
    reserved_tokens: int
    reserved_cost: float
    model_id: str = ""
    status: ReservationStatus = ReservationStatus.RESERVED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 结算字段
    actual_input_tokens: int = 0
    actual_output_tokens: int = 0
    actual_cached_input_tokens: int = 0
    actual_cost: float = 0.0
    usage_estimated: bool = False
    reason_code: str = ""  # ok / usage_missing / exceeded / failed
    settled_at: datetime | None = None

    def settle(self, **updates: Any) -> None:
        for k, v in updates.items():
            setattr(self, k, v)
        self.status = ReservationStatus.SETTLED
        self.settled_at = datetime.now(UTC)

    def release(self) -> None:
        self.status = ReservationStatus.RELEASED
        self.settled_at = datetime.now(UTC)

    @property
    def released_tokens(self) -> int:
        if self.status == ReservationStatus.SETTLED:
            return max(0, self.reserved_tokens - (self.actual_input_tokens + self.actual_output_tokens))
        if self.status == ReservationStatus.RELEASED:
            return self.reserved_tokens
        return 0


# ═══════════════════════════════════════════════════════════════
# 用量（结算口径）
# ═══════════════════════════════════════════════════════════════


@dataclass
class BudgetUsage:
    """单次 run 累计用量（含预留口径）。"""

    steps: int = 0
    tool_calls: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    usage_estimated: bool = False
    failed_or_discarded_tokens: int = 0  # failed/discarded token 浪费量
    retry_tokens: int = 0  # 重试放大 token
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_action_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def elapsed_seconds(self, *, now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        return max(0.0, (current - self.started_at).total_seconds())

    def water_level(self, plan: BudgetPlan) -> float:
        """当前水位（0~1）。取 token / cost / steps / tool_calls 中最高者。"""
        ratios: list[float] = []
        if plan.max_input_tokens > 0:
            ratios.append(self.input_tokens / plan.max_input_tokens)
        if plan.max_output_tokens > 0:
            ratios.append(self.output_tokens / plan.max_output_tokens)
        if plan.max_total_tokens > 0:
            ratios.append(self.total_tokens / plan.max_total_tokens)
        if plan.max_cost_usd > 0:
            ratios.append(self.cost_usd / plan.max_cost_usd)
        if plan.max_steps > 0:
            ratios.append(self.steps / plan.max_steps)
        if plan.max_tool_calls > 0:
            ratios.append(self.tool_calls / plan.max_tool_calls)
        return max(ratios, default=0.0)

    def record_llm_settlement(self, reservation: BudgetReservation) -> None:
        """结算一次 LLM 预留后的用量累计。"""
        self.input_tokens += max(0, reservation.actual_input_tokens)
        self.output_tokens += max(0, reservation.actual_output_tokens)
        self.cached_input_tokens += max(0, reservation.actual_cached_input_tokens)
        self.cost_usd += max(0.0, reservation.actual_cost)
        self.usage_estimated = self.usage_estimated or reservation.usage_estimated
        if reservation.reason_code in ("failed", "usage_missing"):
            waste = (
                reservation.actual_input_tokens
                + reservation.actual_output_tokens
                + reservation.reserved_tokens
            )
            self.failed_or_discarded_tokens += max(0, waste)
        self.last_action_at = datetime.now(UTC)

    def record_failed_tool(self, tokens_used: int) -> None:
        self.failed_or_discarded_tokens += max(0, tokens_used)
        self.last_action_at = datetime.now(UTC)


def _stable_hash(payload: dict[str, Any]) -> str:
    import json

    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════
# ConcurrencyLimiter（四层 semaphore）
# ═══════════════════════════════════════════════════════════════


class ConcurrencyLimiter:
    """按命名 key 管理 asyncio.Semaphore（全局/租户/用户/provider/工具）。

    - 每个 (key) 持有一个独立的 asyncio.Semaphore；
    - 并发上限 <=0 表示不限制（不创建 semaphore）。
    """

    def __init__(self) -> None:
        import asyncio

        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._limits: dict[str, int] = {}
        self._asyncio = asyncio

    def set_limit(self, key: str, limit: int) -> None:
        limit = max(0, int(limit))
        self._limits[key] = limit

    def semaphore(self, key: str):
        import asyncio

        limit = self._limits.get(key, 0)
        if limit <= 0:
            return None
        sem = self._semaphores.get(key)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            self._semaphores[key] = sem
        return sem

    def snapshot(self) -> dict[str, int]:
        return dict(self._limits)


# ═══════════════════════════════════════════════════════════════
# BudgetManager
# ═══════════════════════════════════════════════════════════════


class BudgetManager:
    """预留—结算—释放的预算管理器（单 run 实例）。

    Args:
        plan: 请求级预算计划
        user_id / tenant_id: 分层标识（用于用户级/租户级配额）
        tier_quotas: 额外分层配额（模型级/工具级并发与额度）
        limiter: 四层并发限制器（可选，由 Loop 共享）
        on_event: 预算事件回调（budget_reserved/budget_settled/budget_warning/...），可选
    """

    def __init__(
        self,
        plan: BudgetPlan,
        *,
        user_id: str = "",
        tenant_id: str = "",
        tier_quotas: list[TierQuota] | None = None,
        limiter: ConcurrencyLimiter | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.plan = plan
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.tier_quotas = {f"{q.tier.value}:{q.key}": q for q in (tier_quotas or [])}
        self.limiter = limiter or ConcurrencyLimiter()
        self.on_event = on_event
        self.usage = BudgetUsage()
        self.reservations: list[BudgetReservation] = []
        self.finalization_reserved: bool = False
        self._seq = 0

    # ── 水位 ──────────────────────────────────────────────

    def status(self) -> BudgetStatus:
        """当前预算水位状态。"""
        if not self.can_continue():
            return BudgetStatus.EXHAUSTED
        level = self.usage.water_level(self.plan)
        if level >= self.plan.compress_ratio:
            return BudgetStatus.COMPRESS
        if level >= self.plan.warning_ratio:
            return BudgetStatus.WARNING
        return BudgetStatus.OK

    def water_level(self) -> float:
        return self.usage.water_level(self.plan)

    def can_continue(self) -> bool:
        """是否仍有预算继续（宽松：未到 100% 即视为可继续）。"""
        return not self._exceeded()

    def _exceeded(self) -> list[str]:
        broken: list[str] = []
        p = self.plan
        u = self.usage
        if p.max_input_tokens > 0 and u.input_tokens >= p.max_input_tokens:
            broken.append("max_input_tokens")
        if p.max_output_tokens > 0 and u.output_tokens >= p.max_output_tokens:
            broken.append("max_output_tokens")
        if p.max_total_tokens > 0 and u.total_tokens >= p.max_total_tokens:
            broken.append("max_total_tokens")
        if p.max_cost_usd > 0 and u.cost_usd >= p.max_cost_usd:
            broken.append("max_cost_usd")
        if p.max_steps > 0 and u.steps >= p.max_steps:
            broken.append("max_steps")
        if p.max_tool_calls > 0 and u.tool_calls >= p.max_tool_calls:
            broken.append("max_tool_calls")
        if p.deadline_at is not None and datetime.now(UTC) >= p.deadline_at:
            broken.append("deadline")
        elif p.max_runtime_seconds > 0 and u.elapsed_seconds() >= p.max_runtime_seconds:
            broken.append("max_runtime_seconds")
        return broken

    def exhausted_reason(self) -> str:
        """返回最相关的耗尽原因码。"""
        broken = self._exceeded()
        if not broken:
            return ""
        if "deadline" in broken or "max_runtime_seconds" in broken:
            return "time_budget_exhausted"
        if "max_cost_usd" in broken:
            return "cost_budget_exhausted"
        return "token_budget_exhausted"

    # ── 预留 ──────────────────────────────────────────────

    async def reserve(
        self,
        *,
        kind: ReservationKind,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
        estimated_cost: float = 0.0,
        tool_name: str = "",
        model_id: str = "",
    ) -> BudgetReservation | None:
        """预留预算；预算不足时返回 None（调用方不得发起调用）。"""
        # 100% 水位：停止新工具和新规划；只允许有预留额度的 finalization
        if self._exceeded():
            self._emit("budget_exhausted", {"reason": self.exhausted_reason()})
            return None
        if kind == ReservationKind.FINALIZATION and self.finalization_reserved:
            # 同一 run 只预留一次 finalization
            return None

        reserved_tokens = max(0, estimated_input_tokens) + max(0, estimated_output_tokens)
        # 预估为 0 时按保守上界（水位模型：输入按剩余 30%，输出按 plan 上限的 30%）
        if reserved_tokens <= 0:
            reserve_input = int(self.plan.max_input_tokens * 0.3)
            reserve_output = int(self.plan.max_output_tokens * 0.3)
            reserved_tokens = reserve_input + reserve_output
            reserved_cost = (
                (reserve_input + reserve_output) * 2.0 / 1_000_000
                if self.plan.max_cost_usd > 0
                else 0.0
            )
        else:
            reserved_cost = max(0.0, estimated_cost)

        # 分层配额检查（user / tenant / model / tool）
        quota_broken = self._check_tier_quotas(kind=kind, tool_name=tool_name, model_id=model_id)
        if quota_broken:
            self._emit("budget_denied", {"tier": quota_broken, "kind": kind.value})
            return None

        scope = "request"
        if kind == ReservationKind.TOOL:
            scope = f"tool:{tool_name or 'unknown'}"
        elif kind == ReservationKind.LLM:
            scope = f"model:{model_id or 'unknown'}"

        self._seq += 1
        reservation = BudgetReservation(
            reservation_id=f"rsv-{self._seq}-{time.time_ns() & 0xFFFF}",
            kind=kind,
            scope=scope,
            reserved_tokens=reserved_tokens,
            reserved_cost=reserved_cost,
            model_id=model_id,
        )
        self.reservations.append(reservation)
        if kind == ReservationKind.FINALIZATION:
            self.finalization_reserved = True

        self._emit(
            "budget_reserved",
            {
                "reservation_id": reservation.reservation_id,
                "kind": kind.value,
                "scope": scope,
                "reserved_tokens": reserved_tokens,
                "reserved_cost": round(reserved_cost, 8),
                "water_level": round(self.water_level(), 4),
            },
        )
        return reservation

    def reserve_finalization_nowait(
        self,
        *,
        tool_name: str = "",
        model_id: str = "",
    ) -> BudgetReservation | None:
        """同步预留 finalization（无 await 版本，供 Loop 简化调用）。"""
        # 预留额不足以支撑 finalization 时视为无预算
        if self.plan.finalization_reserve_tokens <= 0:
            return None
        # finalization 复用预留机制：直接构造预留并记录
        self._seq += 1
        scope = f"tool:{tool_name or 'finalization'}"
        reservation = BudgetReservation(
            reservation_id=f"rsv-fz-{self._seq}-{time.time_ns() & 0xFFFF}",
            kind=ReservationKind.FINALIZATION,
            scope=scope,
            reserved_tokens=self.plan.finalization_reserve_tokens,
            reserved_cost=0.0,
            model_id=model_id,
        )
        self.reservations.append(reservation)
        self.finalization_reserved = True
        self._emit(
            "budget_reserved",
            {
                "reservation_id": reservation.reservation_id,
                "kind": ReservationKind.FINALIZATION.value,
                "scope": reservation.scope,
                "reserved_tokens": reservation.reserved_tokens,
                "water_level": round(self.water_level(), 4),
            },
        )
        return reservation

    def has_finalization_reserve(self) -> bool:
        return self.finalization_reserved

    def _check_tier_quotas(
        self,
        *,
        kind: ReservationKind,
        tool_name: str,
        model_id: str,
    ) -> str:
        """分层配额检查；返回被触发的层 key（空 = 通过）。"""
        u = self.usage
        keys = [
            f"{BudgetTier.USER.value}:{self.user_id}" if self.user_id else "",
            f"{BudgetTier.TENANT.value}:{self.tenant_id}" if self.tenant_id else "",
            f"{BudgetTier.MODEL.value}:{model_id}" if model_id else "",
            f"{BudgetTier.TOOL.value}:{tool_name}" if tool_name else "",
        ]
        for key in keys:
            if not key:
                continue
            quota = self.tier_quotas.get(key)
            if quota is None:
                continue
            if quota.max_total_tokens > 0 and u.total_tokens >= quota.max_total_tokens:
                return key
            if quota.max_cost_usd > 0 and u.cost_usd >= quota.max_cost_usd:
                return key
            if quota.max_concurrency > 0:
                sem = self.limiter.semaphore(key)
                if sem is not None and sem.locked():
                    return key
        return ""

    # ── 结算 ──────────────────────────────────────────────

    def settle_llm(
        self,
        reservation: BudgetReservation,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cost_usd: float = 0.0,
        usage_estimated: bool = False,
        reason_code: str = "ok",
        attribution: str = "",  # retry / fallback / cancel / primary
    ) -> None:
        """结算一次 LLM 预留。usage 缺失或异常时按保守估算并记录原因。"""
        if reservation.status != ReservationStatus.RESERVED:
            return
        in_t = max(0, int(input_tokens))
        out_t = max(0, int(output_tokens))
        cached_t = max(0, min(int(cached_input_tokens), in_t))
        if in_t <= 0 and out_t <= 0:
            in_t = reservation.reserved_tokens
            usage_estimated = True
            if reason_code in ("", "ok"):
                reason_code = "usage_missing"
        if cost_usd <= 0 and (in_t + out_t) > 0:
            from agent.pricing_catalog import compute_cost

            cost = compute_cost(
                reservation.model_id or "deepseek-chat",
                input_tokens=in_t,
                output_tokens=out_t,
                cached_input_tokens=cached_t,
                input_tokens_estimated=usage_estimated,
                output_tokens_estimated=usage_estimated,
            )
            cost_usd = cost["cost_usd"]
        reservation.settle(
            actual_input_tokens=in_t,
            actual_output_tokens=out_t,
            actual_cached_input_tokens=cached_t,
            actual_cost=max(0.0, float(cost_usd)),
            usage_estimated=usage_estimated,
            reason_code=reason_code,
        )
        self.usage.record_llm_settlement(reservation)
        if attribution == "retry":
            self.usage.retry_tokens += in_t + out_t
        self._emit(
            "budget_settled",
            {
                "reservation_id": reservation.reservation_id,
                "kind": reservation.kind.value,
                "input_tokens": in_t,
                "output_tokens": out_t,
                "cached_input_tokens": cached_t,
                "cost_usd": round(max(0.0, float(cost_usd)), 8),
                "usage_estimated": usage_estimated,
                "reason_code": reason_code,
                "attribution": attribution,
                "water_level": round(self.water_level(), 4),
            },
        )

    def release(self, reservation: BudgetReservation) -> int:
        """释放预留未使用部分；返回释放的 token 数。"""
        released = reservation.released_tokens
        reservation.release()
        self._emit(
            "budget_released",
            {
                "reservation_id": reservation.reservation_id,
                "released_tokens": released,
            },
        )
        return released

    def record_tool_call(self) -> None:
        self.usage.tool_calls += 1
        self.usage.last_action_at = datetime.now(UTC)

    def record_step(self) -> None:
        self.usage.steps += 1
        self.usage.last_action_at = datetime.now(UTC)

    def record_retry(self, tokens_used: int = 0) -> None:
        self.usage.retries += 1
        self.usage.retry_tokens += max(0, tokens_used)
        self.usage.last_action_at = datetime.now(UTC)

    # ── 事件 ──────────────────────────────────────────────

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event_type, payload)
            except Exception:
                logger.warning("[budget] on_event callback failed for %s", event_type, exc_info=True)

    def to_metrics(self) -> dict[str, Any]:
        """汇总指标（验证指标 5 节）。"""
        return {
            "water_level": round(self.water_level(), 4),
            "status": self.status().value,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cached_input_tokens": self.usage.cached_input_tokens,
            "total_tokens": self.usage.total_tokens,
            "cost_usd": round(self.usage.cost_usd, 8),
            "usage_estimated": self.usage.usage_estimated,
            "steps": self.usage.steps,
            "tool_calls": self.usage.tool_calls,
            "retries": self.usage.retries,
            "retry_tokens": self.usage.retry_tokens,
            "failed_or_discarded_tokens": self.usage.failed_or_discarded_tokens,
            "reservations": len(self.reservations),
            "finalization_reserved": self.finalization_reserved,
        }
