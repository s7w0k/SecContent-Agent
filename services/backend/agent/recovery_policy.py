"""RecoveryPolicy — 错误解决策略引擎（阶段3 WBS 3.6；阶段3 §3）。

输入（RecoveryContext）至少覆盖：
  - error category/code；
  - 当前 phase、step、attempt；
  - tool/model/provider；
  - side-effect level；
  - 剩余 Token、成本、时间和重试预算；
  - 是否存在替代模型/工具；
  - 已完成步骤和现有证据；
  - 用户风险等级和审批状态。

输出限定为 11 种恢复动作之一（RecoveryAction）。策略必须确定、可配置、
可测试，不能完全交由模型自由决定 —— 规则表由代码构造，模型无法修改。

默认规则对齐统一架构 §5 的类别默认策略，并细化重试上限/替代资源/审批升级。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from agent.error_taxonomy import DEFAULT_STRATEGY, ErrorCategory
from pydantic import BaseModel, Field

# 输出动作白名单（阶段3 §3 限定集合）
VALID_ACTIONS = (
    "retry_same",
    "repair_then_retry",
    "switch_model",
    "switch_tool",
    "replan",
    "continue_partial",
    "wait_approval",
    "pause_dependency",
    "dead_letter",
    "compensate",
    "stop_failed",
)


class RecoveryAction(StrEnum):
    """恢复动作（策略输出，限定集合）。"""

    RETRY_SAME = "retry_same"
    REPAIR_THEN_RETRY = "repair_then_retry"
    SWITCH_MODEL = "switch_model"
    SWITCH_TOOL = "switch_tool"
    REPLAN = "replan"
    CONTINUE_PARTIAL = "continue_partial"
    WAIT_APPROVAL = "wait_approval"
    PAUSE_DEPENDENCY = "pause_dependency"
    DEAD_LETTER = "dead_letter"
    COMPENSATE = "compensate"
    STOP_FAILED = "stop_failed"


class RecoveryContext(BaseModel):
    """策略输入（当前故障快照）。"""

    error_category: ErrorCategory
    error_code: str = ""
    phase: str = ""  # plan / policy / execute / observe / validate
    step: str = ""
    attempt: int = 1  # 当前步已尝试次数（含本次）
    tool_name: str = ""
    model_id: str = ""
    provider: str = ""
    side_effect_level: str = "L0"  # L0/L1/L2/L3
    remaining_budget_ok: bool = True  # 剩余 Token/成本/时间/重试预算是否充足
    alternative_models: list[str] = Field(default_factory=list)
    alternative_tools: list[str] = Field(default_factory=list)
    completed_steps: int = 0
    evidence_count: int = 0
    risk_level: str = "L0"  # 用户风险等级
    approval_status: str = "none"  # none / pending / approved / denied

    @property
    def max_attempts(self) -> int:
        """默认重试上限：同一步最多允许的尝试次数。"""
        return 3


class RecoveryDecision(BaseModel):
    """策略输出。"""

    action: RecoveryAction
    reason: str
    max_attempts_left: int = 0  # 允许继续重试的剩余次数
    escalate_to_approval: bool = False  # 是否需要升级人工审批
    compensate: bool = False  # 是否需要补偿动作


class RecoveryRule:
    """单条确定性规则：when 谓词命中则采用该动作。"""

    def __init__(
        self,
        when: Callable[[RecoveryContext], bool],
        action: RecoveryAction,
        reason: str,
        *,
        attempts_left: int = 0,
        escalate_to_approval: bool = False,
        compensate: bool = False,
    ):
        self.when = when
        self.action = action
        self.reason = reason
        self.attempts_left = attempts_left
        self.escalate_to_approval = escalate_to_approval
        self.compensate = compensate


# ── 默认规则表（确定、可配置、可测试）─────────────────────────


def _default_rules() -> dict[ErrorCategory, list[RecoveryRule]]:
    def _has_budget(ctx: RecoveryContext) -> bool:
        return ctx.remaining_budget_ok

    def _attempts_left(ctx: RecoveryContext) -> int:
        return max(0, ctx.max_attempts - ctx.attempt)

    rules: dict[ErrorCategory, list[RecoveryRule]] = {
        # 429/5xx/连接中断：有限重试 → 切模型 → 停止（熔断时 pause）
        ErrorCategory.TRANSIENT_PROVIDER: [
            RecoveryRule(
                lambda c: c.attempt < c.max_attempts and _has_budget(c),
                RecoveryAction.RETRY_SAME,
                "provider 瞬时故障且有预算，有限重试",
                attempts_left=_attempts_left,
            ),
            RecoveryRule(
                lambda c: bool(c.alternative_models) and _has_budget(c),
                RecoveryAction.SWITCH_MODEL,
                "重试耗尽，切换到替代模型",
                attempts_left=1,
            ),
            RecoveryRule(
                lambda c: not _has_budget(c),
                RecoveryAction.CONTINUE_PARTIAL,
                "预算不足，降级继续",
            ),
            RecoveryRule(
                lambda c: True,
                RecoveryAction.STOP_FAILED,
                "provider 持续故障且无替代资源",
            ),
        ],
        # 上下文溢出：压缩/重新规划（不重试原调用）
        ErrorCategory.CONTEXT_OVERFLOW: [
            RecoveryRule(
                lambda c: True,
                RecoveryAction.REPLAN,
                "上下文溢出，压缩并重新规划",
            ),
        ],
        # 非法 Schema：一次结构修复，失败后停止
        ErrorCategory.CONTRACT_ERROR: [
            RecoveryRule(
                lambda c: c.attempt == 1,
                RecoveryAction.REPAIR_THEN_RETRY,
                "JSON/schema 非法，结构修复一次",
                attempts_left=1,
            ),
            RecoveryRule(
                lambda c: True,
                RecoveryAction.STOP_FAILED,
                "结构修复失败，可解释终态",
            ),
        ],
        # 工具瞬时故障：幂等前提重试 → 替代工具 → 部分结果
        ErrorCategory.TOOL_TRANSIENT: [
            RecoveryRule(
                lambda c: c.attempt < c.max_attempts
                and c.side_effect_level in ("L0", "L1")
                and _has_budget(c),
                RecoveryAction.RETRY_SAME,
                "工具瞬时故障且幂等/只读，有限重试",
                attempts_left=_attempts_left,
            ),
            RecoveryRule(
                lambda c: bool(c.alternative_tools) and _has_budget(c),
                RecoveryAction.SWITCH_TOOL,
                "重试耗尽，切换到替代工具",
            ),
            RecoveryRule(
                lambda c: c.attempt >= c.max_attempts and c.evidence_count > 0,
                RecoveryAction.CONTINUE_PARTIAL,
                "已有证据，部分结果继续",
            ),
            RecoveryRule(
                lambda c: c.attempt >= c.max_attempts,
                RecoveryAction.DEAD_LETTER,
                "工具持续不可用，死信等待人工",
            ),
            RecoveryRule(
                lambda c: True,
                RecoveryAction.STOP_FAILED,
                "工具瞬时故障无法恢复",
            ),
        ],
        # 工具永久故障：不重试 → 重新规划或人工处理
        ErrorCategory.TOOL_PERMANENT: [
            RecoveryRule(
                lambda c: bool(c.alternative_tools),
                RecoveryAction.SWITCH_TOOL,
                "参数/权限类永久故障，换替代工具",
            ),
            RecoveryRule(
                lambda c: c.completed_steps > 0,
                RecoveryAction.REPLAN,
                "跳过该步重新规划其余目标",
            ),
            RecoveryRule(
                lambda c: True,
                RecoveryAction.STOP_FAILED,
                "永久故障且无法规避，终止",
            ),
        ],
        # 策略拒绝：可审批则等待审批，否则停止
        ErrorCategory.POLICY_DENIED: [
            RecoveryRule(
                lambda c: c.risk_level in ("L2", "L3") and c.approval_status != "denied",
                RecoveryAction.WAIT_APPROVAL,
                "策略拒绝，升级人工审批",
                escalate_to_approval=True,
            ),
            RecoveryRule(
                lambda c: True,
                RecoveryAction.STOP_FAILED,
                "策略拒绝且不可审批，终止",
            ),
        ],
        # 数据冲突（CAS/版本）：重读后有限重放
        ErrorCategory.DATA_CONFLICT: [
            RecoveryRule(
                lambda c: c.attempt < c.max_attempts,
                RecoveryAction.RETRY_SAME,
                "并发冲突，重读后重试",
                attempts_left=_attempts_left,
            ),
            RecoveryRule(
                lambda c: True,
                RecoveryAction.CONTINUE_PARTIAL,
                "冲突未消解，跳过该步继续",
            ),
        ],
        # 预算耗尽：降级 finalization 或部分结果
        ErrorCategory.BUDGET_EXHAUSTED: [
            RecoveryRule(
                lambda c: c.completed_steps > 0 or c.evidence_count > 0,
                RecoveryAction.CONTINUE_PARTIAL,
                "预算耗尽，保留已完成部分降级终态",
            ),
            RecoveryRule(
                lambda c: True,
                RecoveryAction.STOP_FAILED,
                "预算耗尽且无产出，终止",
            ),
        ],
        # 循环无进展：replan 一次，随后停止
        ErrorCategory.LOOP_NO_PROGRESS: [
            RecoveryRule(
                lambda c: c.attempt == 1,
                RecoveryAction.REPLAN,
                "检测到循环，重新规划一次",
            ),
            RecoveryRule(
                lambda c: True,
                RecoveryAction.STOP_FAILED,
                "replan 后仍无进展，停止",
            ),
        ],
        # 依赖故障（Mongo/Redis/队列）：暂停等待恢复 → 死信
        ErrorCategory.DEPENDENCY_OUTAGE: [
            RecoveryRule(
                lambda c: c.attempt == 1,
                RecoveryAction.PAUSE_DEPENDENCY,
                "依赖中断，暂停等待健康恢复",
            ),
            RecoveryRule(
                lambda c: True,
                RecoveryAction.DEAD_LETTER,
                "依赖持续不可用，死信等待人工",
            ),
        ],
        # 未分类异常：立即终态化，禁止盲重试
        ErrorCategory.INTERNAL_BUG: [
            RecoveryRule(
                lambda c: True,
                RecoveryAction.STOP_FAILED,
                "未分类异常，立即终态化并告警，禁止盲重试",
            ),
        ],
    }
    return rules


class RecoveryPolicy:
    """确定性恢复策略引擎（规则表代码构造，可注入覆盖）。"""

    def __init__(self, rules: dict[ErrorCategory, list[RecoveryRule]] | None = None):
        self._rules = dict(_default_rules() if rules is None else rules)

    def decide(self, context: RecoveryContext) -> RecoveryDecision:
        """按类别规则表顺序匹配第一条命中规则（确定性）。"""
        category = ErrorCategory(context.error_category)
        rules = self._rules.get(category, [])
        if not rules:
            # 无显式规则 → 使用类别默认策略（兜底）
            action = RecoveryAction(DEFAULT_STRATEGY.get(category, "stop_failed"))
            return RecoveryDecision(
                action=action,
                reason=f"无显式规则，使用类别默认策略 {action.value}",
            )
        for rule in rules:
            if rule.when(context):
                attempts_left = (
                    rule.attempts_left(context)
                    if callable(rule.attempts_left)
                    else rule.attempts_left
                )
                return RecoveryDecision(
                    action=rule.action,
                    reason=rule.reason,
                    max_attempts_left=max(0, attempts_left),
                    escalate_to_approval=rule.escalate_to_approval,
                    compensate=rule.compensate,
                )
        return RecoveryDecision(
            action=RecoveryAction.STOP_FAILED,
            reason="策略引擎未命中任何规则，保守停止",
        )

    @property
    def actions(self) -> frozenset[str]:
        """全部规则可能输出的动作集合（门禁校验：必须 ⊆ 白名单）。"""
        out = {rule.action.value for rules in self._rules.values() for rule in rules}
        return frozenset(out)
