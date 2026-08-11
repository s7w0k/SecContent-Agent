"""统一错误分类 — 阶段3 WBS 3.6 前置（统一架构文档 §5）。

ErrorTaxonomy：将所有异常/错误码映射到 11 个稳定错误类别，每个类别有
默认恢复策略。分类是 RecoveryPolicy 决策的第一步，必须确定性、可测试；
未分类异常一律归入 internal_bug（禁止盲重试）。

对齐阶段3 故障注入矩阵：
  - LLM timeout/429/5xx  → transient_provider
  - LLM 非法 Schema     → contract_error
  - 工具 timeout        → tool_transient
  - Mongo/Redis/ARQ 中断 → dependency_outage
  - 重复投递/乱序       → data_conflict
  - 预算/时间耗尽       → budget_exhausted
"""

from __future__ import annotations

from enum import StrEnum

# 错误类别 → 默认恢复策略（对齐统一架构 §5 与阶段3 §3 输出集合）
DEFAULT_STRATEGY: dict[ErrorCategory, str] = {}


class ErrorCategory(StrEnum):
    """统一错误分类（11 类，稳定枚举）。"""

    TRANSIENT_PROVIDER = "transient_provider"  # 429、5xx、连接中断
    CONTEXT_OVERFLOW = "context_overflow"  # 超上下文窗口
    CONTRACT_ERROR = "contract_error"  # JSON/schema 不合法
    TOOL_TRANSIENT = "tool_transient"  # 工具超时、短暂不可用
    TOOL_PERMANENT = "tool_permanent"  # 参数非法、权限不足
    POLICY_DENIED = "policy_denied"  # 越权、高风险
    DATA_CONFLICT = "data_conflict"  # CAS、版本冲突
    BUDGET_EXHAUSTED = "budget_exhausted"  # Token/费用/时间耗尽
    LOOP_NO_PROGRESS = "loop_no_progress"  # 重复动作、无新证据
    DEPENDENCY_OUTAGE = "dependency_outage"  # Mongo/Redis/队列故障
    INTERNAL_BUG = "internal_bug"  # 未分类异常


_DEFAULT_STRATEGY = {
    ErrorCategory.TRANSIENT_PROVIDER: "retry_same",
    ErrorCategory.CONTEXT_OVERFLOW: "replan",
    ErrorCategory.CONTRACT_ERROR: "repair_then_retry",
    ErrorCategory.TOOL_TRANSIENT: "retry_same",
    ErrorCategory.TOOL_PERMANENT: "replan",
    ErrorCategory.POLICY_DENIED: "wait_approval",
    ErrorCategory.DATA_CONFLICT: "retry_same",
    ErrorCategory.BUDGET_EXHAUSTED: "continue_partial",
    ErrorCategory.LOOP_NO_PROGRESS: "replan",
    ErrorCategory.DEPENDENCY_OUTAGE: "pause_dependency",
    ErrorCategory.INTERNAL_BUG: "stop_failed",
}
DEFAULT_STRATEGY.update(_DEFAULT_STRATEGY)


def default_strategy(category: ErrorCategory) -> str:
    return _DEFAULT_STRATEGY[ErrorCategory(category)]


# ── 错误码 → 类别映射（故障注入矩阵全覆盖）────────────────────
_ERROR_CODE_MAP: dict[str, ErrorCategory] = {
    # LLM provider
    "rate_limit": ErrorCategory.TRANSIENT_PROVIDER,  # 429
    "timeout": ErrorCategory.TRANSIENT_PROVIDER,
    "connect_error": ErrorCategory.TRANSIENT_PROVIDER,
    "server_error": ErrorCategory.TRANSIENT_PROVIDER,  # 5xx
    "service_unavailable": ErrorCategory.TRANSIENT_PROVIDER,
    "bad_gateway": ErrorCategory.TRANSIENT_PROVIDER,
    # 上下文 / schema
    "context_length_exceeded": ErrorCategory.CONTEXT_OVERFLOW,
    "token_overflow": ErrorCategory.CONTEXT_OVERFLOW,
    "invalid_schema": ErrorCategory.CONTRACT_ERROR,
    "json_parse_error": ErrorCategory.CONTRACT_ERROR,
    "schema_validation": ErrorCategory.CONTRACT_ERROR,
    # 工具
    "tool_timeout": ErrorCategory.TOOL_TRANSIENT,
    "tool_unavailable": ErrorCategory.TOOL_TRANSIENT,
    "tool_failed": ErrorCategory.TOOL_TRANSIENT,
    "invalid_argument": ErrorCategory.TOOL_PERMANENT,
    "invalid_params": ErrorCategory.TOOL_PERMANENT,
    "permission_denied": ErrorCategory.TOOL_PERMANENT,
    "not_found": ErrorCategory.TOOL_PERMANENT,
    "tool_not_found": ErrorCategory.TOOL_PERMANENT,
    # 策略 / 审批
    "policy_denied": ErrorCategory.POLICY_DENIED,
    "not_in_allowlist": ErrorCategory.POLICY_DENIED,
    "high_risk": ErrorCategory.POLICY_DENIED,
    "missing_idempotency_key": ErrorCategory.POLICY_DENIED,
    # 并发 / 一致性
    "cas_conflict": ErrorCategory.DATA_CONFLICT,
    "state_conflict": ErrorCategory.DATA_CONFLICT,
    "lease_conflict": ErrorCategory.DATA_CONFLICT,
    "version_conflict": ErrorCategory.DATA_CONFLICT,
    # 预算
    "budget_exceeded": ErrorCategory.BUDGET_EXHAUSTED,
    "budget_exhausted": ErrorCategory.BUDGET_EXHAUSTED,
    "deadline_exceeded": ErrorCategory.BUDGET_EXHAUSTED,
    "max_steps": ErrorCategory.BUDGET_EXHAUSTED,
    # 循环
    "loop_detected": ErrorCategory.LOOP_NO_PROGRESS,
    "no_progress": ErrorCategory.LOOP_NO_PROGRESS,
    "repeated_action": ErrorCategory.LOOP_NO_PROGRESS,
    # 依赖
    "mongo_unavailable": ErrorCategory.DEPENDENCY_OUTAGE,
    "redis_unavailable": ErrorCategory.DEPENDENCY_OUTAGE,
    "queue_unavailable": ErrorCategory.DEPENDENCY_OUTAGE,
    "arq_unavailable": ErrorCategory.DEPENDENCY_OUTAGE,
    "dependency_outage": ErrorCategory.DEPENDENCY_OUTAGE,
}

# 内置异常类型 → 类别
_EXC_CATEGORY_BY_TYPE: dict[type, ErrorCategory] = {
    TimeoutError: ErrorCategory.TRANSIENT_PROVIDER,
    ConnectionError: ErrorCategory.TRANSIENT_PROVIDER,
    OSError: ErrorCategory.TRANSIENT_PROVIDER,
}


def classify_error(
    error_code: str = "",
    *,
    exc: BaseException | None = None,
    tool_name: str = "",
    provider: str = "",
) -> ErrorCategory:
    """将异常/错误码映射到稳定错误类别。

    判定优先级：
      1. 已知错误码（覆盖故障注入矩阵）→ 对应类别；
      2. 异常类型白名单（timeout/connection/os）→ transient_provider；
      3. 工具名非空但无类别信息 → tool_transient（保守，可重试判定由策略细化）；
      4. 其余一律 internal_bug（禁止盲重试）。
    """
    code = (error_code or "").strip().lower()
    if code in _ERROR_CODE_MAP:
        return _ERROR_CODE_MAP[code]
    if exc is not None:
        for exc_type, category in _EXC_CATEGORY_BY_TYPE.items():
            if isinstance(exc, exc_type):
                return category
    if tool_name:
        return ErrorCategory.TOOL_TRANSIENT
    return ErrorCategory.INTERNAL_BUG
