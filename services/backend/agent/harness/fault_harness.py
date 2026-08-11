"""Fault Harness — 阶段4 §1.5（WBS 4.2）。

按步骤注入 11 类故障：timeout / exception / 429 / 5xx / 连接中断 / 非法 schema /
进程 kill / lease 过期 / 重复事件 / 乱序事件 / 日志失败。

- FaultInjector：注册 + 触发（错误类抛 FaultInjected，事件/日志类返回 marker）；
- FAULT_SCENARIOS：11 个演练场景；
- FaultDrillRunner：演练编排 + 报告（校验目标步骤能恢复，不遗留 running/不崩溃）。

安全约束：故障注入总开关默认关闭（FAULT_HARNESS_ENABLED=false），仅演练时开启。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# 执行阶段（对齐统一事件契约 phase 枚举）
STEPS = ("plan", "policy", "execute", "observe", "validate", "checkpoint", "finalize")


class FaultType(StrEnum):
    TIMEOUT = "timeout"
    EXCEPTION = "exception"
    RATE_LIMIT_429 = "429"
    SERVER_5XX = "5xx"
    CONNECTION_DROP = "connection_drop"
    INVALID_SCHEMA = "invalid_schema"
    PROCESS_KILL = "process_kill"
    LEASE_EXPIRY = "lease_expiry"
    DUPLICATE_EVENT = "duplicate_event"
    OUT_OF_ORDER_EVENT = "out_of_order_event"
    LOG_FAILURE = "log_failure"


@dataclass(frozen=True)
class FaultSpec:
    """单步骤故障规格。"""

    step: str
    fault_type: FaultType
    error_code: str = ""
    message: str = ""
    delay_ms: float = 0.0
    repeat: int = 1  # 该步骤连续触发次数
    probability: float = 1.0

    def __post_init__(self) -> None:
        if self.step not in STEPS:
            raise ValueError(f"unknown fault step: {self.step}")


class FaultInjected(Exception):
    """错误类故障已注入。"""

    def __init__(self, *, step: str, fault_type: FaultType, error_code: str = "", message: str = ""):
        super().__init__(message or f"fault injected at {step}: {fault_type.value}")
        self.step = step
        self.fault_type = fault_type
        self.error_code = error_code or fault_type.value


class ProcessKilled(FaultInjected):
    """模拟进程被杀（用于演练 reaper/恢复扫描）。"""


# ═══════════════════════════════════════════════════════════════
# 11 类演练场景（阶段4 §1.5）
# ═══════════════════════════════════════════════════════════════

FAULT_SCENARIOS: dict[str, list[FaultSpec]] = {
    "timeout": [FaultSpec("execute", FaultType.TIMEOUT, error_code="timeout", message="tool timeout")],
    "exception": [FaultSpec("execute", FaultType.EXCEPTION, error_code="internal_error", message="unclassified")],
    "rate_limit_429": [FaultSpec("execute", FaultType.RATE_LIMIT_429, error_code="429", message="too many requests")],
    "server_5xx": [FaultSpec("policy", FaultType.SERVER_5XX, error_code="503", message="upstream unavailable")],
    "connection_drop": [FaultSpec("execute", FaultType.CONNECTION_DROP, error_code="connection_drop", message="conn reset")],
    "invalid_schema": [FaultSpec("observe", FaultType.INVALID_SCHEMA, error_code="invalid_schema", message="bad json")],
    "process_kill": [FaultSpec("checkpoint", FaultType.PROCESS_KILL, error_code="process_kill", message="worker killed")],
    "lease_expiry": [FaultSpec("checkpoint", FaultType.LEASE_EXPIRY, error_code="lease_expired", message="lease expired")],
    "duplicate_event": [FaultSpec("checkpoint", FaultType.DUPLICATE_EVENT, error_code="duplicate_event", message="event replayed")],
    "out_of_order_event": [FaultSpec("observe", FaultType.OUT_OF_ORDER_EVENT, error_code="out_of_order", message="sequence gap")],
    "log_failure": [FaultSpec("finalize", FaultType.LOG_FAILURE, error_code="log_failure", message="log write failed")],
}


class FaultInjector:
    """按步骤注册并触发故障。"""

    def __init__(self, *, enabled: bool = True, random_seed: int | None = None):
        self.enabled = enabled
        self._specs: dict[str, list[FaultSpec]] = defaultdict(list)
        self._hits: dict[str, int] = defaultdict(int)
        self._rng = random.Random(random_seed)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def register(self, spec: FaultSpec) -> None:
        self._specs[spec.step].append(spec)

    def register_scenario(self, name: str) -> None:
        if name not in FAULT_SCENARIOS:
            raise ValueError(f"unknown fault scenario: {name}")
        for spec in FAULT_SCENARIOS[name]:
            self.register(spec)

    def reset_hits(self) -> None:
        self._hits.clear()

    def hits(self, step: str | None = None) -> int:
        if step is None:
            return sum(self._hits.values())
        return self._hits.get(step, 0)

    def active(self, step: str) -> FaultSpec | None:
        """返回该步骤当前应生效的 spec（未达重复次数且概率命中）。"""
        if not self.enabled:
            return None
        for spec in self._specs.get(step, []):
            if self._hits[step] >= spec.repeat:
                continue
            if spec.probability < 1.0 and self._rng.random() > spec.probability:
                continue
            return spec
        return None

    async def inject(self, step: str, *, context: dict[str, Any] | None = None) -> str | None:
        """触发该步骤故障。

        Returns:
            None: 无故障；marker 字符串: 事件/日志类故障（由调用方处理）；
        错误类故障直接抛出 FaultInjected（TIMEOUT 抛 asyncio.TimeoutError）。
        """
        spec = self.active(step)
        if spec is None:
            return None
        self._hits[step] += 1
        return await self._trigger(spec)

    async def _trigger(self, spec: FaultSpec) -> str:
        if spec.delay_ms > 0:
            await asyncio.sleep(spec.delay_ms / 1000)
        fault_type = spec.fault_type
        if fault_type == FaultType.TIMEOUT:
            raise TimeoutError(spec.message or "injected timeout")
        if fault_type == FaultType.PROCESS_KILL:
            raise ProcessKilled(step=spec.step, fault_type=fault_type, error_code=spec.error_code, message=spec.message)
        if fault_type in (FaultType.EXCEPTION, FaultType.RATE_LIMIT_429, FaultType.SERVER_5XX, FaultType.CONNECTION_DROP, FaultType.INVALID_SCHEMA):
            raise FaultInjected(step=spec.step, fault_type=fault_type, error_code=spec.error_code, message=spec.message)
        # 事件/日志类：返回 marker，由调用方决定处理方式
        return fault_type.value


# ═══════════════════════════════════════════════════════════════
# 演练
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class DrillStepResult:
    """单步骤演练结果。"""

    step: str
    fault_type: FaultType
    outcome: str  # "passed" | "failed"
    observed: str = ""


@dataclass(frozen=True)
class DrillReport:
    """一次故障演练报告。"""

    scenario: str
    name: str
    passed: bool
    steps: list[DrillStepResult]
    started_at: datetime
    duration_ms: float

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "name": self.name,
            "passed": self.passed,
            "steps": [
                {"step": s.step, "fault_type": s.fault_type.value, "outcome": s.outcome, "observed": s.observed}
                for s in self.steps
            ],
            "started_at": self.started_at.isoformat(),
            "duration_ms": round(self.duration_ms, 2),
        }


class FaultDrillRunner:
    """演练编排：对场景每个步骤故障注入后调用 target 恢复路径并校验。"""

    def __init__(
        self,
        *,
        injector: FaultInjector,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.injector = injector
        self._now = now_provider or (lambda: datetime.now(UTC))

    async def run(
        self,
        *,
        scenario: str,
        target: Callable[[str], Awaitable[bool]],
        name: str = "",
    ) -> DrillReport:
        """target(step) 表示步骤在遭遇注入故障后的恢复路径，返回 True=已恢复。"""
        if scenario not in FAULT_SCENARIOS:
            raise ValueError(f"unknown fault scenario: {scenario}")
        started = time.perf_counter()
        results: list[DrillStepResult] = []
        for spec in FAULT_SCENARIOS[scenario]:
            self.injector.reset_hits()
            recovered = False
            observed = ""
            try:
                recovered = await target(spec.step)
            except ProcessKilled as exc:
                observed = f"process_killed:{exc.error_code or 'process_kill'}"
            except FaultInjected as exc:
                observed = f"{exc.fault_type.value}:{exc.error_code or ''}"
            except TimeoutError:
                observed = "timeout"
            except Exception as exc:
                observed = type(exc).__name__
            results.append(
                DrillStepResult(
                    step=spec.step,
                    fault_type=spec.fault_type,
                    outcome="passed" if recovered else "failed",
                    observed=observed,
                )
            )
        passed = all(r.outcome == "passed" for r in results)
        return DrillReport(
            scenario=scenario,
            name=name or scenario,
            passed=passed,
            steps=results,
            started_at=self._now(),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
