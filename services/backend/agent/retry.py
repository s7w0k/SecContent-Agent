"""统一重试器 -- 阶段一 Step 3。

基于 Step 1 探针结论设计的重试策略：
  - 可重试：APITimeoutError / APIConnectionError / RateLimitError / InternalServerError
  - 不重试：AuthenticationError / BadRequestError / PermissionDeniedError / NotFoundError
  - full jitter 指数退避
  - 所有 attempt 共享绝对 deadline
  - cancel 原样传播

使用方式：
    from agent.retry import with_retry, RetryPolicy

    result = await with_retry(
        lambda: llm.ainvoke(messages),
        policy=RetryPolicy(max_attempts=3, base_delay=1.0),
        trace_id=trace_id,
    )
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("backend.agent.retry")

# ═══════════════════════════════════════════════════════════════
# 异常分类（基于 Step 1 探针结论）
# ═══════════════════════════════════════════════════════════════


# 延迟导入 openai 异常，避免在未安装 openai 时崩溃
def _get_retryable_exceptions() -> tuple[type[Exception], ...]:
    """获取可重试的异常类型。"""
    try:
        from openai import (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )

        return (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)
    except ImportError:
        return (asyncio.TimeoutError, ConnectionError)


def _get_non_retryable_exceptions() -> tuple[type[Exception], ...]:
    """获取不可重试的异常类型。"""
    try:
        from openai import (
            AuthenticationError,
            BadRequestError,
            NotFoundError,
            PermissionDeniedError,
        )

        return (AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError)
    except ImportError:
        return ()


RETRYABLE_EXCEPTIONS = _get_retryable_exceptions()
NON_RETRYABLE_EXCEPTIONS = _get_non_retryable_exceptions()


def is_retryable(exc: Exception) -> bool:
    """判断异常是否可重试。"""
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, RETRYABLE_EXCEPTIONS):
        return True
    # 内置超时和连接错误也可重试（Python 3.13 中 asyncio.TimeoutError = TimeoutError）
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    # httpx 超时和连接错误也可重试
    try:
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
            return True
    except ImportError:
        pass
    return False


def is_non_retryable(exc: Exception) -> bool:
    """判断异常是否明确不可重试。"""
    return isinstance(exc, NON_RETRYABLE_EXCEPTIONS)


# ═══════════════════════════════════════════════════════════════
# RetryPolicy
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略配置。

    Attributes:
        max_attempts: 最大尝试次数（含首次，如 3 = 1 次初始 + 2 次重试）
        base_delay: 基础延迟秒数
        multiplier: 退避倍数
        max_delay: 最大延迟秒数
        jitter: 抖动比例（0-1，1=full jitter）
        deadline_at: 绝对截止时间戳（秒），超时不再重试
    """

    max_attempts: int = 3
    base_delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 10.0
    jitter: float = 1.0
    deadline_at: float | None = None


@dataclass
class RetryState:
    """重试状态记录（可变，用于日志和审计）。"""

    attempts: list[dict[str, Any]] = field(default_factory=list)

    def record_attempt(
        self, *, attempt: int, error_type: str, delay: float, decided_by: str
    ) -> None:
        self.attempts.append(
            {
                "attempt": attempt,
                "error_type": error_type,
                "delay": round(delay, 3),
                "decided_by": decided_by,
            }
        )

    @property
    def total_attempts(self) -> int:
        return len(self.attempts)

    @property
    def last_error_type(self) -> str:
        return self.attempts[-1]["error_type"] if self.attempts else ""


# ═══════════════════════════════════════════════════════════════
# with_retry
# ═══════════════════════════════════════════════════════════════


async def with_retry(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    policy: RetryPolicy,
    retry_state: RetryState | None = None,
    trace_id: str = "",
) -> Any:
    """带重试的协程执行器。

    Args:
        coro_factory: 每次调用返回一个新的协程（不是已有协程对象）
        policy: 重试策略
        retry_state: 重试状态记录（可选，用于审计）
        trace_id: 追踪 ID（日志用）

    Returns:
        协程的返回值

    Raises:
        最后一个异常（如果所有重试都失败）
    """
    state = retry_state or RetryState()
    last_exc: Exception | None = None

    for attempt in range(1, policy.max_attempts + 1):
        # 检查绝对 deadline
        if policy.deadline_at is not None:
            remaining = policy.deadline_at - time.monotonic()
            if remaining <= 0:
                state.record_attempt(
                    attempt=attempt,
                    error_type=type(last_exc).__name__ if last_exc else "deadline",
                    delay=0,
                    decided_by="deadline_exceeded",
                )
                logger.warning(
                    "[%s] retry deadline exceeded after %d attempts", trace_id, attempt - 1
                )
                if last_exc:
                    raise last_exc
                raise TimeoutError("retry deadline exceeded")

        try:
            return await coro_factory()
        except asyncio.CancelledError:
            # cancel 原样传播，不重试
            raise
        except Exception as exc:
            last_exc = exc

            if is_non_retryable(exc):
                state.record_attempt(
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    delay=0,
                    decided_by="non_retryable",
                )
                logger.warning(
                    "[%s] non-retryable error: %s: %s", trace_id, type(exc).__name__, str(exc)[:100]
                )
                raise

            if attempt >= policy.max_attempts:
                state.record_attempt(
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    delay=0,
                    decided_by="max_attempts",
                )
                logger.warning(
                    "[%s] max attempts (%d) reached, last error: %s",
                    trace_id,
                    policy.max_attempts,
                    type(exc).__name__,
                )
                raise

            if not is_retryable(exc):
                state.record_attempt(
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    delay=0,
                    decided_by="non_retryable_unknown",
                )
                raise

            # 计算退避延迟（full jitter）
            base = min(policy.base_delay * (policy.multiplier ** (attempt - 1)), policy.max_delay)
            delay = random.uniform(0, base * policy.jitter) if policy.jitter > 0 else base

            # 如果剩余时间不够等延迟，直接重试不等待
            if policy.deadline_at is not None:
                remaining = policy.deadline_at - time.monotonic()
                if delay > remaining:
                    delay = min(delay, max(0, remaining))

            state.record_attempt(
                attempt=attempt,
                error_type=type(exc).__name__,
                delay=delay,
                decided_by="retryable",
            )

            logger.info(
                "[%s] retryable error on attempt %d/%d: %s, waiting %.2fs",
                trace_id,
                attempt,
                policy.max_attempts,
                type(exc).__name__,
                delay,
            )

            await asyncio.sleep(delay)

    # 理论上不会到达
    if last_exc:
        raise last_exc
    raise RuntimeError("retry loop exited unexpectedly")
