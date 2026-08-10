"""Agent Loop 公共契约 -- 阶段一 Step 2。

定义 Agent Loop 运行时所需的数据结构：
  - RunContext：单次运行的上下文（身份、权限、预算边界）
  - LoopBudget / BudgetUsage：预算与用量
  - ToolPolicy / TypedToolResult：工具策略与结构化结果
  - LoopStatus / LoopResult：Loop 运行状态与结果
  - AgentEvent：步级事件

设计约束：
  - user_id 是唯一稳定身份字段，tenant_id 仅未来可选
  - allowed_article_hashes / allowed_product_ids 由服务端生成，模型参数中不提供身份
  - 所有 dataclass/frozen=True，防止运行中被篡改
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ═══════════════════════════════════════════════════════════════
# RunContext
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class RunContext:
    """单次 Agent 运行的上下文（不可变）。

    由 API 层创建，传入 AgentLoop。模型不接触此对象。

    Attributes:
        trace_id: 全链路追踪 ID（沿用现有 logging_config.get_trace_id）
        run_id: 本次 Agent 运行的唯一 ID（uuid4）
        user_id: 当前用户 ID（来自 Depends(get_current_user)）
        request_id: HTTP 请求 ID（可选）
        allowed_article_hashes: 允许读取的文章 url_hash 白名单
        allowed_product_ids: 允许查询的产品 ID 白名单
        deadline_at: 运行截止时间（UTC），超时自动终止
        tenant_id: 未来多租户预留（当前不用于授权判断）
    """

    trace_id: str
    run_id: str
    user_id: str
    request_id: str = ""
    allowed_article_hashes: frozenset[str] = field(default_factory=frozenset)
    allowed_product_ids: frozenset[str] = field(default_factory=frozenset)
    deadline_at: datetime | None = None
    tenant_id: str | None = None

    def is_article_allowed(self, url_hash: str) -> bool:
        """检查文章是否在白名单内。"""
        if not self.allowed_article_hashes:
            return False
        return url_hash in self.allowed_article_hashes

    def is_product_allowed(self, product_id: str) -> bool:
        """检查产品是否在白名单内。"""
        if not self.allowed_product_ids:
            return False
        return product_id in self.allowed_product_ids

    def is_expired(self, now: datetime | None = None) -> bool:
        """检查是否已过截止时间。"""
        if self.deadline_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        return current >= self.deadline_at


# ═══════════════════════════════════════════════════════════════
# Budget
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LoopBudget:
    """Agent Loop 预算上限（不可变）。

    从 settings 读取，每次运行创建一份。
    """

    max_rounds: int = 5
    max_input_tokens: int = 24000
    max_output_tokens: int = 4000
    max_tool_calls: int = 8
    max_parallel_tools: int = 3
    deadline_seconds: int = 30
    tool_timeout_seconds: int = 5
    max_cost_usd: float = 0.0  # 0 = 不限制


@dataclass
class BudgetUsage:
    """Agent Loop 预算用量（可变，运行中累加）。"""

    rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def can_continue(self, budget: LoopBudget, *, now: datetime | None = None) -> bool:
        """检查是否还有预算继续运行。"""
        if self.rounds >= budget.max_rounds:
            return False
        if self.input_tokens >= budget.max_input_tokens:
            return False
        if self.output_tokens >= budget.max_output_tokens:
            return False
        if self.tool_calls >= budget.max_tool_calls:
            return False
        if budget.max_cost_usd > 0 and self.cost_usd >= budget.max_cost_usd:
            return False
        current = now or datetime.now(timezone.utc)
        elapsed = (current - self.started_at).total_seconds()
        if elapsed >= budget.deadline_seconds:
            return False
        return True

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ═══════════════════════════════════════════════════════════════
# Tool Policy & Result
# ═══════════════════════════════════════════════════════════════


class ToolPermission(str, Enum):
    """工具执行权限级别。"""

    ALLOWED = "allowed"
    DENIED = "denied"
    DENIED_NOT_IN_ALLOWLIST = "denied_not_in_allowlist"
    DENIED_POLICY = "denied_policy"


@dataclass(frozen=True)
class ToolPolicy:
    """单个工具的执行策略（不可变）。

    Attributes:
        name: 工具显示名（脱敏，不暴露内部函数名）
        idempotent: 是否幂等（幂等工具可自动重试）
        timeout_seconds: 执行超时
        requires_article_allowlist: 是否需要文章白名单校验
        requires_product_allowlist: 是否需要产品白名单校验
    """

    name: str
    idempotent: bool = True
    timeout_seconds: int = 5
    requires_article_allowlist: bool = False
    requires_product_allowlist: bool = False


@dataclass(frozen=True)
class TypedToolResult:
    """工具执行的结构化结果（不可变）。

    所有工具统一返回此结构，模型与校验器统一消费。

    Attributes:
        ok: 是否成功
        data: 结果数据（字符串，已按预算截断）
        error: 错误信息（ok=False 时填充）
        error_code: 错误码（如 'db_unavailable', 'not_found', 'permission_denied'）
        truncated: 结果是否被截断
        source_ids: 数据来源 ID（如知识文档 ID、记忆项 ID）
        char_count: 原始结果字符数（截断前）
    """

    ok: bool
    data: str = ""
    error: str = ""
    error_code: str = ""
    truncated: bool = False
    source_ids: list[str] = field(default_factory=list)
    char_count: int = 0

    def to_tool_message_content(self) -> str:
        """转为 ToolMessage 内容（模型可见）。"""
        if not self.ok:
            return f"[工具执行失败] error_code={self.error_code}, error={self.error}"
        suffix = " (结果已截断)" if self.truncated else ""
        return self.data + suffix

    @classmethod
    def success(
        cls,
        data: str,
        *,
        source_ids: list[str] | None = None,
        truncated: bool = False,
        char_count: int | None = None,
    ) -> TypedToolResult:
        return cls(
            ok=True,
            data=data,
            source_ids=source_ids or [],
            truncated=truncated,
            char_count=char_count if char_count is not None else len(data),
        )

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        error_code: str = "unknown",
    ) -> TypedToolResult:
        return cls(ok=False, error=error, error_code=error_code)


# ═══════════════════════════════════════════════════════════════
# Loop Status & Result
# ═══════════════════════════════════════════════════════════════


class LoopStatus(str, Enum):
    """Agent Loop 运行状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
    MAX_ROUNDS_REACHED = "max_rounds_reached"


@dataclass
class LoopResult:
    """Agent Loop 运行结果。

    Attributes:
        status: 终态状态
        answer: 最终回答文本
        rounds: 实际执行轮次
        usage: 预算用量
        references: 实际使用的上下文来源 ID
        events: 步级事件列表
        degraded: 是否降级
        degrade_reason: 降级原因
        tool_names_used: 使用过的工具名列表
    """

    status: LoopStatus
    answer: str = ""
    rounds: int = 0
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    references: list[str] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
    degraded: bool = False
    degrade_reason: str = ""
    tool_names_used: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否成功完成（非降级、非失败）。"""
        return self.status == LoopStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════
# Agent Event
# ═══════════════════════════════════════════════════════════════


class EventType(str, Enum):
    """Agent 事件类型。"""

    LOOP_START = "loop_start"
    LOOP_END = "loop_end"
    ROUND_START = "round_start"
    ROUND_END = "round_end"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    TOOL_BLOCKED = "tool_blocked"
    TOOL_FAILED = "tool_failed"
    BUDGET_WARNING = "budget_warning"
    DEGRADE = "degrade"
    CANCEL = "cancel"
    FINALIZATION = "finalization"


@dataclass(frozen=True)
class AgentEvent:
    """步级事件（不可变，落库用）。

    安全约束：
      - 不保存 prompt、完整 args/result、用户正文、私有推理
      - 只保存工具显示名、状态、错误码、duration、args/result hash
    """

    type: EventType
    sequence: int
    run_id: str
    trace_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tool_name: str = ""
    tool_args_hash: str = ""
    tool_result_hash: str = ""
    error_code: str = ""
    duration_ms: int = 0
    round_no: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> dict[str, Any]:
        """转为可落库的字典（脱敏）。"""
        return {
            "type": self.type.value,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "timestamp": self.timestamp.isoformat(),
            "tool_name": self.tool_name,
            "tool_args_hash": self.tool_args_hash,
            "tool_result_hash": self.tool_result_hash,
            "error_code": self.error_code,
            "duration_ms": self.duration_ms,
            "round_no": self.round_no,
        }
