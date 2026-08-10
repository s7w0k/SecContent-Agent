"""ModelRouter 单元测试 — 阶段四 4A Step 4A-5。

覆盖：默认模型优先、敏感等级过滤、上下文长度过滤、预算过滤、
回退链、降级禁止、无候选模型、路由日志脱敏。
"""

from __future__ import annotations

import pytest

from agent.model_router import (
    ModelCapability,
    ModelRouter,
    ModelRoutingError,
    RouteRequest,
    SensitivityLevel,
    TaskType,
)


def _models() -> list[ModelCapability]:
    return [
        ModelCapability(
            name="deepseek-chat",
            max_sensitivity=SensitivityLevel.L1,
            max_context_chars=12000,
            min_input_tokens=2000,
            quality=4,
        ),
        ModelCapability(
            name="deepseek-reasoner",
            max_sensitivity=SensitivityLevel.L2,
            max_context_chars=32000,
            min_input_tokens=4000,
            quality=5,
        ),
        ModelCapability(
            name="cheap-lite",
            max_sensitivity=SensitivityLevel.L0,
            max_context_chars=8000,
            min_input_tokens=500,
            quality=1,
        ),
    ]


def _router(**kw) -> ModelRouter:
    base = dict(
        models=_models(),
        default_model="deepseek-chat",
        fallback_chain=("cheap-lite", "deepseek-reasoner"),
    )
    base.update(kw)
    return ModelRouter(**base)


class TestRouting:
    def test_routes_to_default_when_all_filters_pass(self):
        decision = _router().route(
            RouteRequest(task_type=TaskType.PLAN, context_chars=5000)
        )
        assert decision.model == "deepseek-chat"
        assert not decision.degraded
        assert decision.reason_code == "primary"
        assert decision.log == []  # 未降级不产生路由日志

    def test_sensitivity_gate_blocks_untrusted_models(self):
        # L2 数据：只有 deepseek-reasoner（max_sensitivity=L2）可达
        decision = _router().route(
            RouteRequest(task_type=TaskType.EXECUTE, sensitivity=SensitivityLevel.L2)
        )
        assert decision.model == "deepseek-reasoner"
        assert decision.degraded
        assert decision.reason_code == "fallback_chain"

    def test_context_too_large_falls_back(self):
        # 上下文 20000 字符：deepseek-chat(12000)/cheap-lite(8000) 均不满足
        decision = _router().route(
            RouteRequest(task_type=TaskType.VALIDATE, context_chars=20000)
        )
        assert decision.model == "deepseek-reasoner"
        assert decision.degraded
        assert decision.reason_code == "fallback_chain"

    def test_budget_poor_prefers_smaller_model(self):
        # 剩余输入 token 1000：deepseek-chat(2000)/reasoner(4000) 超出，仅 cheap-lite 可负担
        decision = _router().route(
            RouteRequest(task_type=TaskType.DECIDE, remaining_input_tokens=1000)
        )
        assert decision.model == "cheap-lite"
        assert decision.degraded

    def test_downgrade_disallowed_raises(self):
        with pytest.raises(ModelRoutingError):
            _router().route(
                RouteRequest(
                    task_type=TaskType.EXECUTE,
                    sensitivity=SensitivityLevel.L2,
                    allow_downgrade=False,
                )
            )

    def test_no_candidate_raises(self):
        # L3 数据：所有模型均未授权 → 无候选
        with pytest.raises(ModelRoutingError):
            _router().route(
                RouteRequest(task_type=TaskType.EXECUTE, sensitivity=SensitivityLevel.L3)
            )

    def test_fallback_chain_order_respected(self):
        # 上下文 10000：cheap-lite(8000) 排除；默认 deepseek-chat 满足 → primary
        decision = _router().route(
            RouteRequest(task_type=TaskType.PLAN, context_chars=10000)
        )
        assert decision.model == "deepseek-chat"
        assert not decision.degraded


class TestRouteLog:
    def test_log_contains_only_model_reason_and_usage(self):
        decision = _router().route(
            RouteRequest(
                task_type=TaskType.EXECUTE,
                sensitivity=SensitivityLevel.L2,
                remaining_input_tokens=5000,
                remaining_output_tokens=1000,
                remaining_cost_usd=0.5,
            )
        )
        assert decision.log  # 降级必有日志
        entry = decision.log[0]
        assert set(entry.keys()) <= {
            "model",
            "reason_code",
            "remaining_input_tokens",
            "remaining_output_tokens",
            "remaining_cost_usd",
        }
        # 日志不含任何正文/内容字段
        assert not any("content" in k or "prompt" in k for k in entry)

    def test_deterministic_routing(self):
        req = RouteRequest(task_type=TaskType.DECIDE, context_chars=5000)
        d1 = _router().route(req)
        d2 = _router().route(req)
        assert d1.model == d2.model
        assert d1.reason_code == d2.reason_code
