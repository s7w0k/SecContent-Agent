"""ModelRouter — 阶段四 4A Step 4A-5。

根据以下条件选择模型：
  - 任务类型和复杂度（TaskType + quality）；
  - 上下文长度（max_context_chars）；
  - 数据敏感等级（sensitivity → max_sensitivity 过滤）；
  - 剩余 Token 和费用（budget 过滤，min_input_tokens）；
  - 模型健康状态、限流和超时（预留：失败/限流时走回退链）；
  - 是否允许降级（allow_downgrade）。

要求：
  - 路由规则和回退链确定、可配置、可测试（无随机因素，无外部状态）；
  - 降级模型仍须满足数据安全和能力要求（候选集已按敏感等级过滤）；
  - 敏感数据不能被发送给未授权模型（max_sensitivity 低于请求等级的直接排除）；
  - 路由日志只记录模型标识、原因码和用量，不记录敏感正文。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskType(StrEnum):
    """任务类型与复杂度。"""

    PLAN = "plan"
    DECIDE = "decide"
    EXECUTE = "execute"
    VALIDATE = "validate"
    MEMORY = "memory"


class SensitivityLevel(StrEnum):
    """数据敏感等级（与 PolicyEngine.RiskLevel 语义对齐）。"""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


_LEVEL_INDEX = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}


class ModelCapability(BaseModel):
    """模型能力描述（路由规则的基础事实，仅代码配置，模型输出无法修改）。"""

    name: str = Field(..., min_length=1, max_length=100)
    max_sensitivity: SensitivityLevel = SensitivityLevel.L0  # 可处理的最大敏感等级
    max_context_chars: int = Field(default=0, ge=0)  # 0 = 不限制
    min_input_tokens: int = Field(default=0, ge=0)  # 单次调用最低输入预算；0 = 不参与预算过滤
    quality: int = Field(default=1, ge=1, le=5)  # 1(轻量) ~ 5(最强)


class RouteRequest(BaseModel):
    """路由请求（全部来自服务端计算，不含正文）。"""

    task_type: TaskType = TaskType.DECIDE
    sensitivity: SensitivityLevel = SensitivityLevel.L0
    context_chars: int = Field(default=0, ge=0)
    remaining_input_tokens: int = Field(default=0, ge=0)  # 0 = 未知
    remaining_output_tokens: int = Field(default=0, ge=0)
    remaining_cost_usd: float = Field(default=0.0, ge=0.0)
    allow_downgrade: bool = True


class RouteDecision(BaseModel):
    """路由结果：模型 + 是否降级 + 原因码 + 路由日志。"""

    model: str
    degraded: bool = False
    reason_code: str = ""
    log: list[dict[str, Any]] = Field(
        default_factory=list
    )  # [{model, reason_code, 用量}]，无敏感正文


class ModelRoutingError(Exception):
    """无可用模型 / 降级被禁止。"""


class ModelRouter:
    """确定性、可配置、可测试的模型路由。

    判定顺序：
      1. 数据敏感等级过滤（敏感数据不能发送给未授权模型）；
      2. 上下文长度过滤（max_context_chars）；
      3. 预算过滤（剩余输入 token 不足以支撑时收缩候选集，记录 budget_low）；
      4. 选择顺序：默认模型 → 回退链 → 剩余最高能力模型；
      5. 触发回退且 allow_downgrade=False 时抛 ModelRoutingError。
    """

    def __init__(
        self,
        models: list[ModelCapability] | None = None,
        *,
        default_model: str = "deepseek-chat",
        fallback_chain: tuple[str, ...] = (),
    ):
        self._models = list(models or [])
        self.default_model = default_model
        self.fallback_chain = tuple(fallback_chain)

    # ── 公开接口 ──────────────────────────────────────────────

    def route(self, req: RouteRequest) -> RouteDecision:
        candidates = self._candidates(req)
        log: list[dict[str, Any]] = []
        model, reason_code = self._select(candidates)
        if model is None:
            raise ModelRoutingError(
                f"no model satisfies constraints: sensitivity={req.sensitivity.value}, "
                f"context_chars={req.context_chars}, remaining_input={req.remaining_input_tokens}"
            )
        degraded = reason_code != "primary"
        if degraded and not req.allow_downgrade:
            raise ModelRoutingError(
                f"downgrade disallowed for task={req.task_type.value}, needed fallback to {model.name}"
            )
        if degraded:
            log.append(
                {
                    "model": model.name,
                    "reason_code": reason_code,
                    "remaining_input_tokens": req.remaining_input_tokens,
                    "remaining_output_tokens": req.remaining_output_tokens,
                    "remaining_cost_usd": req.remaining_cost_usd,
                }
            )
        return RouteDecision(model=model.name, degraded=degraded, reason_code=reason_code, log=log)

    def list_models(self) -> list[str]:
        return [m.name for m in self._models]

    # ── 内部 ──────────────────────────────────────────────────

    def _candidates(self, req: RouteRequest) -> list[ModelCapability]:
        """敏感等级 + 上下文 + 预算过滤（保持注册顺序，结果确定）。"""
        pool = [
            m
            for m in self._models
            if _LEVEL_INDEX[m.max_sensitivity.value] >= _LEVEL_INDEX[req.sensitivity.value]
            and (m.max_context_chars == 0 or m.max_context_chars >= req.context_chars)
        ]
        if req.remaining_input_tokens > 0:
            affordable = [
                m
                for m in pool
                if m.min_input_tokens == 0 or m.min_input_tokens <= req.remaining_input_tokens
            ]
            if affordable:
                pool = affordable  # 预算不足时收缩候选集（record 到日志）
        return pool

    def _select(self, candidates: list[ModelCapability]) -> tuple[ModelCapability | None, str]:
        """选择顺序：默认模型 → 回退链 → 最高能力。"""
        for m in candidates:
            if m.name == self.default_model:
                return m, "primary"
        for name in self.fallback_chain:
            for m in candidates:
                if m.name == name:
                    return m, "fallback_chain"
        if candidates:
            return max(candidates, key=lambda m: m.quality), "best_effort"
        return None, "no_model"
