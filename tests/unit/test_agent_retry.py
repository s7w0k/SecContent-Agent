"""retry.py 单元测试 -- 阶段一 Step 3。"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from agent.retry import (
    RetryPolicy,
    RetryState,
    is_non_retryable,
    is_retryable,
    with_retry,
)


class TestRetryableClassification:
    """异常分类测试。"""

    def test_asyncio_timeout_is_retryable(self):
        assert is_retryable(TimeoutError())

    def test_connection_error_is_retryable(self):
        assert is_retryable(ConnectionError())

    def test_cancelled_error_not_retryable(self):
        assert not is_retryable(asyncio.CancelledError())

    def test_value_error_not_retryable(self):
        assert not is_retryable(ValueError("bad"))

    def test_key_error_not_retryable(self):
        assert not is_retryable(KeyError("missing"))

    def test_openai_timeout_is_retryable(self):
        try:
            from openai import APITimeoutError

            assert is_retryable(APITimeoutError(request=None))  # type: ignore
        except ImportError:
            pytest.skip("openai not installed")

    def test_openai_auth_error_not_retryable(self):
        try:
            from openai import AuthenticationError

            # AuthenticationError 需要一个带 request 属性的 response 对象
            mock_response = MagicMock()
            mock_response.request = MagicMock()
            exc = AuthenticationError(message="test", response=mock_response, body=None)
            assert is_non_retryable(exc)
        except (ImportError, TypeError):
            pytest.skip("openai AuthenticationError constructor not compatible")


class TestWithRetry:
    """with_retry 行为测试。"""

    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        call_count = 0

        async def coro():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await with_retry(coro, policy=RetryPolicy(max_attempts=3), trace_id="t1")
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_timeout(self):
        call_count = 0

        async def coro():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TimeoutError()
            return "ok"

        result = await with_retry(
            coro,
            policy=RetryPolicy(max_attempts=3, base_delay=0.01, multiplier=1.0, max_delay=0.01),
            trace_id="t1",
        )
        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_value_error(self):
        call_count = 0

        async def coro():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad request")

        with pytest.raises(ValueError):
            await with_retry(coro, policy=RetryPolicy(max_attempts=3), trace_id="t1")
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_max_attempts_exhausted(self):
        call_count = 0

        async def coro():
            nonlocal call_count
            call_count += 1
            raise TimeoutError()

        with pytest.raises(asyncio.TimeoutError):
            await with_retry(
                coro,
                policy=RetryPolicy(max_attempts=3, base_delay=0.01, multiplier=1.0, max_delay=0.01),
                trace_id="t1",
            )
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_cancel_propagates(self):
        async def coro():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await with_retry(coro, policy=RetryPolicy(max_attempts=3), trace_id="t1")

    @pytest.mark.asyncio
    async def test_retry_state_records_attempts(self):
        state = RetryState()

        async def coro():
            raise TimeoutError()

        with pytest.raises(asyncio.TimeoutError):
            await with_retry(
                coro,
                policy=RetryPolicy(max_attempts=2, base_delay=0.01, multiplier=1.0, max_delay=0.01),
                retry_state=state,
                trace_id="t1",
            )

        assert state.total_attempts == 2
        assert state.last_error_type == "TimeoutError"
        assert state.attempts[0]["decided_by"] == "retryable"
        assert state.attempts[1]["decided_by"] == "max_attempts"

    @pytest.mark.asyncio
    async def test_full_jitter_delays(self):
        """验证 jitter 产生的延迟在 [0, base*multiplier^(n-1)] 范围内。"""

        async def coro():
            raise TimeoutError()

        state = RetryState()
        with pytest.raises(asyncio.TimeoutError):
            await with_retry(
                coro,
                policy=RetryPolicy(
                    max_attempts=3,
                    base_delay=0.5,
                    multiplier=2.0,
                    max_delay=2.0,
                    jitter=1.0,
                ),
                retry_state=state,
                trace_id="t1",
            )

        # attempt 1: delay in [0, 0.5], attempt 2: delay in [0, 1.0]
        assert len(state.attempts) == 3  # 2 retries + 1 final
        assert state.attempts[0]["delay"] <= 0.5
        assert state.attempts[1]["delay"] <= 1.0

    @pytest.mark.asyncio
    async def test_deadline_stops_retry(self):
        """deadline 过期后不执行调用。"""
        call_count = 0

        async def coro():
            nonlocal call_count
            call_count += 1
            raise TimeoutError()

        # deadline 已过期
        policy = RetryPolicy(
            max_attempts=5,
            base_delay=0.01,
            deadline_at=time.monotonic() - 1,  # 1 秒前已过期
        )

        with pytest.raises(asyncio.TimeoutError):
            await with_retry(coro, policy=policy, trace_id="t1")
        # deadline 过期，不执行任何调用
        assert call_count == 0
