"""熔断与隔离 — 阶段3 §4（Circuit Breaker）。

按 provider、模型、工具、远端 Agent 建立独立熔断器：
  - closed / open / half-open 三态；
  - 滑动窗口错误率与超时率（固定窗口，窗口内失败数超阈值 → open）；
  - half-open 探测配额（放行少量探测请求验证恢复）；
  - 熔断期间使用明确 fallback，不允许请求堆积；
  - CircuitBreakerRegistry 按 key 提供隔离的熔断器实例。

与 budget_manager.ConcurrencyLimiter（并发隔离）互补：Limiter 控制同时
在飞请求数，Breaker 控制"某依赖是否值得继续尝试"。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"  # 正常：全部放行
    OPEN = "open"  # 熔断：拒绝新请求，等待冷却
    HALF_OPEN = "half_open"  # 半开：放行探测配额


@dataclass
class CircuitConfig:
    """熔断器配置（每个 key 独立）。"""

    failure_threshold: int = 5  # 窗口内失败数达到即 open
    timeout_threshold: int = 3  # 窗口内超时数达到即 open（超时也计入失败）
    window_size: int = 20  # 滑动窗口记录数
    cooldown_seconds: float = 30.0  # open 保持时间，之后转 half-open
    half_open_quota: int = 1  # half-open 放行的探测请求数


@dataclass
class CircuitSnapshot:
    key: str
    state: CircuitState
    failure_count: int
    success_count: int
    timeout_count: int
    last_failure_at: float | None


@dataclass
class _Record:
    ok: bool
    timed_out: bool = False


class CircuitOpenError(RuntimeError):
    """熔断打开：请求被拒绝（调用方应使用 fallback）。"""


class CircuitBreaker:
    """单 key 熔断器（非线程安全；asyncio 单事件循环内使用）。"""

    def __init__(
        self,
        key: str,
        config: CircuitConfig | None = None,
        *,
        now_provider: Callable[[], float] | None = None,
    ):
        self.key = key
        self.config = config or CircuitConfig()
        self._now = now_provider or time.monotonic
        self._window: deque[_Record] = deque(maxlen=self.config.window_size)
        self._state = CircuitState.CLOSED
        self._opened_at: float | None = None
        self._half_open_used = 0

    # ── 状态查询 ────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        # 只返回当前三态；OPEN→HALF_OPEN 的冷却期转换仅在 allow_request
        # （真正要放行探测请求）时触发，避免"看一眼状态"就悄悄变半开。
        return self._state

    def snapshot(self, *, now: float | None = None) -> CircuitSnapshot:
        self._maybe_advance_half_open(now=now)
        return CircuitSnapshot(
            key=self.key,
            state=self._state,
            failure_count=sum(1 for r in self._window if not r.ok),
            success_count=sum(1 for r in self._window if r.ok),
            timeout_count=sum(1 for r in self._window if r.timed_out),
            last_failure_at=self._opened_at,
        )

    # ── 放行判定 ───────────────────────────────────────────

    def allow_request(self, *, now: float | None = None) -> bool:
        """当前是否允许发起请求（open 拒绝；half-open 仅放行探测配额）。"""
        state = self._maybe_advance_half_open(now=now)
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.OPEN:
            return False
        # HALF_OPEN：配额内放行
        if self._half_open_used < self.config.half_open_quota:
            self._half_open_used += 1
            return True
        return False

    # ── 结果记录 ───────────────────────────────────────────

    def record_success(self) -> None:
        self._window.append(_Record(ok=True))
        if self._state == CircuitState.HALF_OPEN:
            # 探测成功 → 恢复 closed
            self._state = CircuitState.CLOSED
            self._opened_at = None
            self._half_open_used = 0
            self._window.clear()

    def record_failure(self, *, timed_out: bool = False) -> None:
        now = time.monotonic()
        self._window.append(_Record(ok=False, timed_out=timed_out))
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._opened_at = now
            self._half_open_used = 0
            return
        # closed：窗口内失败数/超时数达阈值 → open
        failures = sum(1 for r in self._window if not r.ok)
        timeouts = sum(1 for r in self._window if r.timed_out)
        if failures >= self.config.failure_threshold or (
            self.config.timeout_threshold > 0 and timeouts >= self.config.timeout_threshold
        ):
            self._state = CircuitState.OPEN
            self._opened_at = now

    # ── 保护调用 ───────────────────────────────────────────

    async def call(
        self,
        coro_factory: Callable[[], Awaitable[T]],
        *,
        fallback: Callable[[str], T] | None = None,
        timeout: float | None = None,
    ) -> T:
        """带熔断保护调用：open 时直接 fallback 或抛 CircuitOpenError。"""
        if not self.allow_request():
            if fallback is not None:
                return fallback(self.key)
            raise CircuitOpenError(f"circuit open for {self.key}")
        try:
            if timeout is not None:
                import asyncio

                result = await asyncio.wait_for(coro_factory(), timeout=timeout)
            else:
                result = await coro_factory()
        except Exception as exc:
            is_timeout = isinstance(exc, TimeoutError)
            self.record_failure(timed_out=is_timeout)
            if fallback is not None:
                return fallback(self.key)
            raise
        self.record_success()
        return result

    # ── 内部 ───────────────────────────────────────────────

    def _maybe_advance_half_open(self, *, now: float | None = None) -> CircuitState:
        """open 冷却期满 → half_open（重置探测配额）。"""
        if (
            self._state == CircuitState.OPEN
            and self._opened_at is not None
            and (now or time.monotonic()) - self._opened_at >= self.config.cooldown_seconds
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_used = 0
        return self._state


class CircuitBreakerRegistry:
    """按 provider / model / tool / 远端 Agent 键隔离的熔断器集合。"""

    def __init__(self, config: CircuitConfig | None = None):
        self.config = config or CircuitConfig()
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, key: str) -> CircuitBreaker:
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(key, self.config)
        return self._breakers[key]

    def snapshots(self) -> list[CircuitSnapshot]:
        return [b.snapshot() for b in self._breakers.values()]

    def open_all(self) -> None:
        """故障演练：打开全部熔断器（Fault Harness 用）。"""
        for breaker in self._breakers.values():
            breaker.record_failure(timed_out=False)
            for _ in range(max(0, breaker.config.failure_threshold - 1)):
                breaker.record_failure(timed_out=False)

    @classmethod
    def provider_key(cls, provider: str, model_id: str = "") -> str:
        return f"provider:{provider}" + (f":{model_id}" if model_id else "")

    @classmethod
    def tool_key(cls, tool_name: str) -> str:
        return f"tool:{tool_name}"

    @classmethod
    def agent_key(cls, agent_id: str) -> str:
        return f"agent:{agent_id}"
