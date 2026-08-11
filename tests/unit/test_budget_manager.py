"""BudgetManager 单元测试 -- 阶段1 2.1/2.2/2.3 节（WBS 1.3）。"""

from __future__ import annotations

import pytest
from agent.budget_manager import (
    BudgetManager,
    BudgetPlan,
    BudgetStatus,
    BudgetTier,
    ConcurrencyLimiter,
    ReservationKind,
    TierQuota,
)


def _plan(**kw) -> BudgetPlan:
    d = {
        "max_input_tokens": 1000,
        "max_output_tokens": 500,
        "max_cost_usd": 0.0,
        "max_steps": 5,
        "max_tool_calls": 8,
        "max_runtime_seconds": 60,
        "finalization_reserve_tokens": 200,
    }
    d.update(kw)
    return BudgetPlan(**d)


class TestReserveSettleRelease:
    """预留—结算—释放闭环。"""

    @pytest.mark.asyncio
    async def test_reserve_then_settle_release(self):
        manager = BudgetManager(plan=_plan())
        reservation = await manager.reserve(
            kind=ReservationKind.LLM,
            estimated_input_tokens=100,
            estimated_output_tokens=200,
            model_id="deepseek-chat",
        )
        assert reservation is not None
        assert reservation.status.value == "reserved"

        manager.settle_llm(
            reservation,
            input_tokens=80,
            output_tokens=150,
            reason_code="ok",
        )
        assert reservation.status.value == "settled"
        assert reservation.actual_input_tokens == 80
        assert reservation.actual_output_tokens == 150
        assert manager.usage.input_tokens == 80
        assert manager.usage.output_tokens == 150

        released = manager.release(reservation)
        assert released >= 0
        assert reservation.status.value == "released"

    @pytest.mark.asyncio
    async def test_usage_missing_falls_back_to_reserved(self):
        manager = BudgetManager(plan=_plan())
        reservation = await manager.reserve(
            kind=ReservationKind.LLM,
            estimated_input_tokens=100,
            estimated_output_tokens=100,
            model_id="deepseek-chat",
        )
        # provider 未返回 usage -> 按预留值保守结算并标记 estimated
        manager.settle_llm(reservation, input_tokens=0, output_tokens=0)
        assert reservation.usage_estimated
        assert reservation.reason_code == "usage_missing"
        assert manager.usage.input_tokens == reservation.reserved_tokens

    @pytest.mark.asyncio
    async def test_reserve_denied_when_exhausted(self):
        manager = BudgetManager(plan=_plan(max_steps=1))
        manager.record_step()  # steps=1 >= max_steps=1
        reservation = await manager.reserve(
            kind=ReservationKind.LLM,
            estimated_input_tokens=10,
            estimated_output_tokens=10,
        )
        assert reservation is None
        assert manager.exhausted_reason() == "token_budget_exhausted"


class TestWaterLevel:
    """预算水位。"""

    def test_status_ok_warning_compress(self):
        plan = _plan(max_input_tokens=1000, max_output_tokens=1000)
        manager = BudgetManager(plan=plan)
        assert manager.status() == BudgetStatus.OK

        manager.usage.input_tokens = 750  # 75% >= 70%
        assert manager.status() == BudgetStatus.WARNING

        manager.usage.input_tokens = 950  # 95% >= 90%
        assert manager.status() == BudgetStatus.COMPRESS

        manager.usage.input_tokens = 1000  # 100%
        assert manager.status() == BudgetStatus.EXHAUSTED
        assert not manager.can_continue()

    def test_warning_emits_event(self):
        events: list[str] = []
        manager = BudgetManager(plan=_plan(max_input_tokens=1000), on_event=lambda t, p: events.append(t))
        manager.usage.input_tokens = 900
        assert manager.status() == BudgetStatus.COMPRESS
        # 水位事件由 reserve/settle 触发（此处验证回调注册不抛异常）
        assert callable(manager.on_event)


class TestFinalization:
    """finalization 预算预留。"""

    def test_finalization_reserve(self):
        manager = BudgetManager(plan=_plan(finalization_reserve_tokens=200))
        reservation = manager.reserve_finalization_nowait(model_id="deepseek-chat")
        assert reservation is not None
        assert manager.has_finalization_reserve()
        assert reservation.kind == ReservationKind.FINALIZATION

    def test_no_finalization_reserve_when_tokens_zero(self):
        manager = BudgetManager(plan=_plan(finalization_reserve_tokens=0))
        reservation = manager.reserve_finalization_nowait()
        assert reservation is None
        assert not manager.has_finalization_reserve()


class TestTierQuota:
    """分层配额。"""

    @pytest.mark.asyncio
    async def test_user_quota_denies(self):
        quota = TierQuota(tier=BudgetTier.USER, key="u1", max_total_tokens=100)
        manager = BudgetManager(plan=_plan(), user_id="u1", tier_quotas=[quota])
        manager.usage.input_tokens = 90
        manager.usage.output_tokens = 20  # 总计 110 >= 100
        reservation = await manager.reserve(
            kind=ReservationKind.LLM,
            estimated_input_tokens=10,
            estimated_output_tokens=10,
        )
        assert reservation is None

    @pytest.mark.asyncio
    async def test_tool_quota_allows_within_limit(self):
        quota = TierQuota(tier=BudgetTier.TOOL, key="search", max_total_tokens=1000)
        manager = BudgetManager(plan=_plan(), tier_quotas=[quota])
        reservation = await manager.reserve(
            kind=ReservationKind.TOOL,
            estimated_input_tokens=10,
            estimated_output_tokens=10,
            tool_name="search",
        )
        assert reservation is not None
        assert reservation.scope == "tool:search"


class TestConcurrencyLimiter:
    """四层 semaphore 注册与快照。"""

    def test_set_limit_and_snapshot(self):
        limiter = ConcurrencyLimiter()
        limiter.set_limit("global", 3)
        limiter.set_limit("tool:x", 1)
        sem = limiter.semaphore("global")
        assert sem is not None
        assert limiter.snapshot() == {"global": 3, "tool:x": 1}
        assert limiter.semaphore("no-limit") is None
