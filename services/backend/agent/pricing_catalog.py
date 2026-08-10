"""版本化 LLM 价格表（pricing catalog）。

阶段 0 要求：价格表不得散落在代码常量中，统一在此版本化登记。

- usage 优先采用 provider 返回值；缺失时使用保守估算并标记 usage_estimated=true。
- 实际值与估算值分别统计：文档同时记录 usage_estimated（token 是否估算）与
  pricing_source（价格条目来源，official / internal-estimate）。
- 每次修改价格条目必须自增 PRICING_CATALOG_VERSION，并同步更新 baseline-manifest。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# 价格表版本（修改价格条目时自增；同时写入 baseline-manifest.json 的 pricing_version）
PRICING_CATALOG_VERSION = "2026-08-10.1"

# 兜底保守估算单价（USD / 百万 token）——catalog 未覆盖的模型使用。
# 高于已知官方价，确保成本估算偏保守。
_FALLBACK_INPUT_PRICE_PER_MILLION = 1.0
_FALLBACK_CACHED_INPUT_PRICE_PER_MILLION = 0.1
_FALLBACK_OUTPUT_PRICE_PER_MILLION = 2.0
_FALLBACK_CURRENCY = "USD"
_FALLBACK_SOURCE = "internal-estimate"


@dataclass(frozen=True)
class PricingEntry:
    """一条价格记录。"""

    provider: str
    model_id: str
    effective_from: str  # ISO 日期（yyyy-mm-dd）
    input_price_per_million: float  # 缓存未命中输入价（USD / 百万 token）
    cached_input_price_per_million: float  # 缓存命中输入价（USD / 百万 token）
    output_price_per_million: float  # 输出价（USD / 百万 token）
    currency: str = "USD"
    source: str = "official"  # official=官方文档 / internal-estimate=内部估算


# 价格目录（按 effective_from 升序；查询时取 <= as_of 的最新条目）。
# 数据源：DeepSeek 官方 Models & Pricing（https://api-docs.deepseek.com/quick_start/pricing）
PRICING_CATALOG: tuple[PricingEntry, ...] = (
    PricingEntry(
        provider="deepseek",
        model_id="deepseek-chat",
        effective_from="2025-02-09",
        input_price_per_million=0.27,
        cached_input_price_per_million=0.07,
        output_price_per_million=1.10,
        currency="USD",
        source="https://api-docs.deepseek.com/quick_start/pricing",
    ),
    PricingEntry(
        provider="deepseek",
        model_id="deepseek-v4-flash",
        effective_from="2026-04-23",
        input_price_per_million=0.14,
        cached_input_price_per_million=0.0028,
        output_price_per_million=0.28,
        currency="USD",
        source="https://api-docs.deepseek.com/quick_start/pricing",
    ),
    PricingEntry(
        provider="deepseek",
        model_id="deepseek-v4-pro",
        effective_from="2026-04-23",
        input_price_per_million=0.435,
        cached_input_price_per_million=0.003625,
        output_price_per_million=0.87,
        currency="USD",
        source="https://api-docs.deepseek.com/quick_start/pricing",
    ),
)


def lookup_price(model_id: str, as_of: str | None = None) -> PricingEntry | None:
    """按模型 ID 查询当前生效的价格条目。

    Args:
        model_id: 模型 ID（如 deepseek-chat）
        as_of: ISO 日期（默认今天）；返回 effective_from <= as_of 的最新条目。

    Returns:
        生效的 PricingEntry；目录中无匹配时返回 None（调用方使用保守估算）。
    """
    if as_of is None:
        as_of = date.today().isoformat()
    candidate: PricingEntry | None = None
    for entry in PRICING_CATALOG:
        if entry.model_id == model_id and entry.effective_from <= as_of:
            candidate = entry
    return candidate


def compute_cost(
    model_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    input_tokens_estimated: bool = False,
    output_tokens_estimated: bool = False,
) -> dict:
    """计算一次 LLM 调用的估算成本（USD）。

    Args:
        model_id: 模型 ID
        input_tokens: 输入 token 数
        output_tokens: 输出 token 数
        cached_input_tokens: 缓存命中输入 token 数（0 表示未知，保守按未命中计费）
        input_tokens_estimated / output_tokens_estimated: token 是否由估算得到

    Returns:
        成本明细 dict：cost_usd / currency / pricing_version / pricing_source /
        pricing_estimated（价格条目是否为内部估算）/ usage_estimated（token 是否估算）。
    """
    input_tokens = max(0, int(input_tokens))
    output_tokens = max(0, int(output_tokens))
    cached_input_tokens = max(0, min(int(cached_input_tokens), input_tokens))

    entry = lookup_price(model_id)
    usage_estimated = bool(input_tokens_estimated or output_tokens_estimated)

    if entry is None:
        # 目录未覆盖：使用保守估算单价（高于已知官方价）
        cost = (
            input_tokens * _FALLBACK_INPUT_PRICE_PER_MILLION
            + output_tokens * _FALLBACK_OUTPUT_PRICE_PER_MILLION
        ) / 1_000_000
        return {
            "cost_usd": round(cost, 8),
            "currency": _FALLBACK_CURRENCY,
            "pricing_version": PRICING_CATALOG_VERSION,
            "pricing_source": _FALLBACK_SOURCE,
            "pricing_estimated": True,
            "usage_estimated": usage_estimated,
        }

    cost = (
        (input_tokens - cached_input_tokens) * entry.input_price_per_million
        + cached_input_tokens * entry.cached_input_price_per_million
        + output_tokens * entry.output_price_per_million
    ) / 1_000_000
    return {
        "cost_usd": round(cost, 8),
        "currency": entry.currency,
        "pricing_version": PRICING_CATALOG_VERSION,
        "pricing_source": entry.source,
        "pricing_estimated": False,
        "usage_estimated": usage_estimated,
    }
