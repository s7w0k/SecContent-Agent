"""版本化 pricing catalog 单元测试 -- 阶段 0。

覆盖：价格查询（版本切换）、成本计算（含缓存折扣）、
目录未覆盖模型的保守估算、usage_estimated 标记。
"""

from __future__ import annotations

from agent.pricing_catalog import (
    PRICING_CATALOG,
    PRICING_CATALOG_VERSION,
    compute_cost,
    lookup_price,
)


class TestLookupPrice:
    def test_finds_current_entry(self):
        entry = lookup_price("deepseek-chat")
        assert entry is not None
        assert entry.provider == "deepseek"
        assert entry.currency == "USD"
        assert entry.input_price_per_million == 0.27
        assert entry.output_price_per_million == 1.10

    def test_effective_from_versioning(self):
        # v4-flash 在 2026-04-23 前不可用
        assert lookup_price("deepseek-v4-flash", as_of="2026-04-22") is None
        entry = lookup_price("deepseek-v4-flash", as_of="2026-04-23")
        assert entry is not None
        assert entry.model_id == "deepseek-v4-flash"
        assert entry.input_price_per_million == 0.14
        assert entry.cached_input_price_per_million == 0.0028

    def test_unknown_model_returns_none(self):
        assert lookup_price("gpt-unknown") is None

    def test_catalog_version_present(self):
        assert PRICING_CATALOG_VERSION
        assert PRICING_CATALOG  # 目录非空


class TestComputeCost:
    def test_official_price_reconcilable(self):
        cost = compute_cost(
            "deepseek-chat",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # 1M 输入 * 0.27 + 1M 输出 * 1.10 = 1.37 USD（金额可对账）
        assert cost["cost_usd"] == 0.27 + 1.10
        assert cost["currency"] == "USD"
        assert cost["pricing_version"] == PRICING_CATALOG_VERSION
        assert cost["pricing_estimated"] is False
        assert cost["usage_estimated"] is False
        assert cost["pricing_source"].startswith("https://")

    def test_cached_input_discount(self):
        cost = compute_cost(
            "deepseek-chat",
            input_tokens=1_000_000,
            cached_input_tokens=1_000_000,
            output_tokens=0,
        )
        # 全缓存命中：1M * 0.07 = 0.07 USD
        assert cost["cost_usd"] == 0.07

    def test_usage_estimated_flag(self):
        cost = compute_cost(
            "deepseek-chat",
            input_tokens=1000,
            output_tokens=500,
            input_tokens_estimated=True,
        )
        assert cost["usage_estimated"] is True

    def test_fallback_conservative_estimate(self):
        cost = compute_cost(
            "unknown-model",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # 保守估算：1M * 1.0 + 1M * 2.0 = 3.0 USD（高于已知官方价）
        assert cost["cost_usd"] == 3.0
        assert cost["pricing_estimated"] is True
        assert cost["pricing_source"] == "internal-estimate"

    def test_negative_tokens_clamped(self):
        cost = compute_cost("deepseek-chat", input_tokens=-5, output_tokens=1000)
        assert cost["cost_usd"] >= 0

    def test_actual_vs_estimated_tracked_separately(self):
        actual = compute_cost("deepseek-chat", input_tokens=1000, output_tokens=500)
        estimated = compute_cost(
            "deepseek-chat",
            input_tokens=1000,
            output_tokens=500,
            input_tokens_estimated=True,
        )
        # 实际值与估算值仅 usage_estimated 不同，价格口径一致
        assert actual["cost_usd"] == estimated["cost_usd"]
        assert actual["usage_estimated"] is False
        assert estimated["usage_estimated"] is True
