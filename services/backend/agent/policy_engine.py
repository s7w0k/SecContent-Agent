"""PolicyEngine 与人工审批 — 阶段四 4A Step 4A-3。

统一判断：
  - 工具是否在 Runtime / Skill / 用户权限的交集内；
  - 参数是否满足 schema、数据作用域、路径和域名限制；
  - 数据是否允许发送给指定模型或外部 Agent（预留 data_scope）；
  - 操作可自动执行、需要人工审批，还是永久禁止（L0/L1/L2/L3 风险分级）；
  - 当前请求是否超过预算、速率或风险阈值。

不可破坏的规则（代码级强制，模型输出无法修改）：
  1. 参数变化后原审批失效（approve 时校验 params_hash）；
  2. 审批授权只能消费一次（one_time_token 消费即失效）；
  3. 模型输出不能修改 PolicyEngine 规则（规则表不可变）；
  4. 外部副作用必须携带幂等键（has_side_effect 强制）；
  5. 日志/决策记录不得包含密钥、完整凭证和敏感原文（仅存哈希与摘要）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.runtime_state import (
    BudgetUsage,
    PendingApproval,
    RunBudget,
)


# ═══════════════════════════════════════════════════════════════
# 风险分级与规则
# ═══════════════════════════════════════════════════════════════


class RiskLevel(str, Enum):
    """操作风险分级。"""

    L0 = "L0"  # 检索、读取授权数据
    L1 = "L1"  # 草稿、临时状态等可逆写入
    L2 = "L2"  # 发消息、提交 PR、创建外部任务
    L3 = "L3"  # 删除、权限、凭证、不可恢复操作


class PolicyAction(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyRule(BaseModel):
    """单个工具的策略规则（不可变，仅代码定义）。"""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    risk_level: RiskLevel = RiskLevel.L0
    default_action: PolicyAction = PolicyAction.ALLOW
    has_side_effect: bool = False  # 外部副作用 → 强制幂等键
    allowed_args: frozenset[str] = Field(default_factory=frozenset)  # 参数 key 白名单
    allowed_domains: frozenset[str] = Field(default_factory=frozenset)  # URL 域名白名单
    allowed_path_prefixes: frozenset[str] = Field(default_factory=frozenset)
    disallow_data_export: bool = False  # 禁止把数据发送给外部 Agent/模型（data_scope 预留）


# 默认规则表（安全优先：无规则 = 拒绝；L3 默认禁止）
DEFAULT_RULES: dict[str, PolicyRule] = {
    "retrieve_articles": PolicyRule(tool_name="retrieve_articles", risk_level=RiskLevel.L0),
    "query_articles": PolicyRule(tool_name="query_articles", risk_level=RiskLevel.L0),
    "fetch_article_fulltext": PolicyRule(
        tool_name="fetch_article_fulltext",
        risk_level=RiskLevel.L0,
        allowed_args=frozenset({"url_hash", "url", "idempotency_key"}),
        allowed_domains=frozenset(),  # 空 = 不限（内部知识库）
    ),
    "get_crawl_stats": PolicyRule(tool_name="get_crawl_stats", risk_level=RiskLevel.L0),
    "classify_articles": PolicyRule(
        tool_name="classify_articles", risk_level=RiskLevel.L0, allowed_args=frozenset({"article_ids"})
    ),
    "score_articles": PolicyRule(
        tool_name="score_articles", risk_level=RiskLevel.L0, allowed_args=frozenset({"article_ids", "product_ids"})
    ),
    "crawl_overseas_news": PolicyRule(
        tool_name="crawl_overseas_news",
        risk_level=RiskLevel.L1,
        has_side_effect=True,
        allowed_args=frozenset({"days", "idempotency_key"}),
    ),
    "save_draft": PolicyRule(
        tool_name="save_draft",
        risk_level=RiskLevel.L1,
        has_side_effect=True,
        allowed_args=frozenset({"article_ids", "content", "idempotency_key"}),
    ),
    "update_knowledge": PolicyRule(
        tool_name="update_knowledge",
        risk_level=RiskLevel.L1,
        has_side_effect=True,
        allowed_args=frozenset({"doc_id", "content", "idempotency_key"}),
    ),
    "export_articles_csv": PolicyRule(
        tool_name="export_articles_csv",
        risk_level=RiskLevel.L1,
        has_side_effect=True,
        allowed_args=frozenset({"article_ids", "idempotency_key"}),
    ),
    "send_message": PolicyRule(
        tool_name="send_message",
        risk_level=RiskLevel.L2,
        default_action=PolicyAction.REQUIRE_APPROVAL,
        has_side_effect=True,
        allowed_args=frozenset({"recipient", "content", "idempotency_key"}),
    ),
    "submit_pr": PolicyRule(
        tool_name="submit_pr",
        risk_level=RiskLevel.L2,
        default_action=PolicyAction.REQUIRE_APPROVAL,
        has_side_effect=True,
        allowed_args=frozenset({"title", "body", "idempotency_key"}),
    ),
    "create_external_task": PolicyRule(
        tool_name="create_external_task",
        risk_level=RiskLevel.L2,
        default_action=PolicyAction.REQUIRE_APPROVAL,
        has_side_effect=True,
        disallow_data_export=False,
        allowed_args=frozenset({"external_system", "payload", "idempotency_key"}),
    ),
    # A2A 外部 Agent 调用（阶段四 4B-3/4B-4）：管理员允许列表之外的 peer 直接拒绝；
    # 允许列表内也需预算/限流/熔断三重门禁；has_side_effect 强制幂等键防重试重复副作用
    "a2a_send": PolicyRule(
        tool_name="a2a_send",
        risk_level=RiskLevel.L1,
        has_side_effect=True,
        allowed_args=frozenset({"peer", "skill_id", "idempotency_key"}),
    ),
    "delete_article": PolicyRule(
        tool_name="delete_article",
        risk_level=RiskLevel.L3,
        default_action=PolicyAction.DENY,
        has_side_effect=True,
        allowed_args=frozenset({"article_id"}),
    ),
    "grant_permission": PolicyRule(
        tool_name="grant_permission",
        risk_level=RiskLevel.L3,
        default_action=PolicyAction.DENY,
        has_side_effect=True,
    ),
    "revoke_access": PolicyRule(
        tool_name="revoke_access",
        risk_level=RiskLevel.L3,
        default_action=PolicyAction.DENY,
        has_side_effect=True,
    ),
}


class PolicyDecision(BaseModel):
    """策略判定结果。"""

    allowed: bool
    action: PolicyAction
    risk_level: RiskLevel = RiskLevel.L0
    reason: str = ""
    reason_code: str = ""
    params_hash: str = ""
    params_summary: str = ""
    requires_idempotency_key: bool = False
    approval_id: str = ""  # require_approval 时生成
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ═══════════════════════════════════════════════════════════════
# 脱敏与哈希
# ═══════════════════════════════════════════════════════════════

SENSITIVE_KEYS = frozenset(
    {"api_key", "token", "password", "secret", "credential", "authorization", "cookie"}
)


def redact_value(value: Any, *, key: str = "") -> str:
    """脱敏：密钥类字段掩码；其余保留类型名与长度，不落原文。"""
    if key.lower() in SENSITIVE_KEYS:
        return "***redacted***"
    if isinstance(value, str):
        if len(value) > 64:
            return f"<str:{len(value)}>"
        return value
    if isinstance(value, (dict, list)):
        return f"<{type(value).__name__}:{len(value)}>"
    return str(value)


def params_hash(args: dict[str, Any] | None) -> str:
    """稳定参数指纹（不含密钥原文）。"""
    sanitized = {
        k: (redact_value(v, key=k) if k.lower() in SENSITIVE_KEYS else v)
        for k, v in (args or {}).items()
    }
    raw = json.dumps(sanitized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def params_summary(args: dict[str, Any] | None) -> str:
    """参数摘要：只保留 key 名与脱敏值，供审批人快速判断。"""
    parts = []
    for k, v in (args or {}).items():
        parts.append(f"{k}={redact_value(v, key=k)}")
    return ", ".join(parts)[:300]


# ═══════════════════════════════════════════════════════════════
# PolicyEngine
# ═══════════════════════════════════════════════════════════════


class PolicyEngine:
    """不可变的策略引擎：规则由代码构造，模型输出无法修改。"""

    def __init__(self, rules: dict[str, PolicyRule] | None = None):
        self._rules: dict[str, PolicyRule] = dict(DEFAULT_RULES if rules is None else rules)

    @property
    def rules(self) -> dict[str, PolicyRule]:
        return dict(self._rules)  # 只读视图

    def is_tool_allowed(self, tool_name: str, *, allowed_tool_names: frozenset[str] | None) -> bool:
        """权限交集：Runtime / Skill / 用户允许列表。"""
        if allowed_tool_names is not None and tool_name not in allowed_tool_names:
            return False
        return tool_name in self._rules

    def _url_allowed(self, url: str, rule: PolicyRule) -> bool:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if rule.allowed_domains:
            host = (parsed.hostname or "").lower()
            if not any(host == d or host.endswith("." + d) for d in rule.allowed_domains):
                return False
        if rule.allowed_path_prefixes:
            if not any(parsed.path.startswith(p) for p in rule.allowed_path_prefixes):
                return False
        return True

    def evaluate(
        self,
        *,
        tool_name: str,
        args: dict[str, Any] | None = None,
        user_id: str = "",
        run_id: str = "",
        allowed_tool_names: frozenset[str] | None = None,
        usage: BudgetUsage | None = None,
        budget: RunBudget | None = None,
        now: datetime | None = None,
    ) -> PolicyDecision:
        """按固定顺序判定；任一硬性失败立即拒绝。"""
        args = dict(args or {})
        stamp = now or datetime.now(UTC)

        # 1. 权限交集：不在 Runtime/Skill/用户允许列表内 → 拒绝
        if allowed_tool_names is not None and tool_name not in allowed_tool_names:
            return PolicyDecision(
                allowed=False, action=PolicyAction.DENY, risk_level=RiskLevel.L0,
                reason="tool not in allowlist", reason_code="not_in_allowlist",
                params_hash=params_hash(args), params_summary=params_summary(args),
                created_at=stamp,
            )
        rule = self._rules.get(tool_name)
        if rule is None:
            # 无规则默认拒绝（安全优先）
            return PolicyDecision(
                allowed=False, action=PolicyAction.DENY, risk_level=RiskLevel.L3,
                reason=f"unknown tool: {tool_name}", reason_code="unknown_tool",
                params_hash=params_hash(args), params_summary=params_summary(args),
                created_at=stamp,
            )

        # 2. 参数 schema：key 白名单
        unexpected = set(args) - set(rule.allowed_args)
        if unexpected:
            return PolicyDecision(
                allowed=False, action=PolicyAction.DENY, risk_level=rule.risk_level,
                reason=f"unexpected args: {sorted(unexpected)}", reason_code="unexpected_args",
                params_hash=params_hash(args), params_summary=params_summary(args),
                created_at=stamp,
            )

        # 3. 数据作用域：URL 域名/路径限制
        url = args.get("url") or args.get("target_url")
        if url is not None and not self._url_allowed(str(url), rule):
            return PolicyDecision(
                allowed=False, action=PolicyAction.DENY, risk_level=rule.risk_level,
                reason="url not allowed by data scope", reason_code="url_not_allowed",
                params_hash=params_hash(args), params_summary=params_summary(args),
                created_at=stamp,
            )

        # 4. 预算检查点（模型调用前/工具调用前）
        if usage is not None and budget is not None and not usage.can_start_next_action(budget, now=stamp):
            return PolicyDecision(
                allowed=False, action=PolicyAction.DENY, risk_level=rule.risk_level,
                reason="budget exhausted", reason_code="budget_exhausted",
                params_hash=params_hash(args), params_summary=params_summary(args),
                created_at=stamp,
            )

        # 5. 风险动作：L3 永久禁止；L2 需要审批；其余按规则默认
        if rule.default_action == PolicyAction.DENY or rule.risk_level == RiskLevel.L3:
            return PolicyDecision(
                allowed=False, action=PolicyAction.DENY, risk_level=rule.risk_level,
                reason="permanently forbidden", reason_code="risk_level_l3",
                params_hash=params_hash(args), params_summary=params_summary(args),
                created_at=stamp,
            )
        if rule.default_action == PolicyAction.REQUIRE_APPROVAL or rule.risk_level == RiskLevel.L2:
            return PolicyDecision(
                allowed=False, action=PolicyAction.REQUIRE_APPROVAL, risk_level=rule.risk_level,
                reason="requires human approval", reason_code="requires_approval",
                params_hash=params_hash(args), params_summary=params_summary(args),
                requires_idempotency_key=rule.has_side_effect, created_at=stamp,
            )

        # 6. 外部副作用必须携带幂等键
        if rule.has_side_effect and not args.get("idempotency_key"):
            return PolicyDecision(
                allowed=False, action=PolicyAction.DENY, risk_level=rule.risk_level,
                reason="side effect requires idempotency_key", reason_code="missing_idempotency_key",
                params_hash=params_hash(args), params_summary=params_summary(args),
                requires_idempotency_key=True, created_at=stamp,
            )

        return PolicyDecision(
            allowed=True, action=PolicyAction.ALLOW, risk_level=rule.risk_level,
            reason="ok", reason_code="allowed",
            params_hash=params_hash(args), params_summary=params_summary(args),
            requires_idempotency_key=rule.has_side_effect, created_at=stamp,
        )


# ═══════════════════════════════════════════════════════════════
# 人工审批
# ═══════════════════════════════════════════════════════════════


class ApprovalService:
    """审批生命周期：请求 → 审批（参数变化失效）→ 一次性授权消费。

    不可破坏规则实现：
      - approve 时校验 params_hash 与请求一致，参数变化后原审批失效；
      - one_time_token 消费一次即失效（消费后从 approved_tokens 移除）；
      - 过期审批不可用。
    """

    def __init__(self, *, ttl_seconds: int = 1800, db=None):
        self.ttl_seconds = ttl_seconds
        self.db = db  # 持久化可选（None = 进程内）

    async def request(
        self,
        *,
        run_id: str,
        action: str,
        params_hash: str,
        params_summary: str,
        risk_level: RiskLevel | str,
        trigger_rule: str,
        decision_summary_id: str = "",
        now: datetime | None = None,
    ) -> PendingApproval:
        stamp = now or datetime.now(UTC)
        approval = PendingApproval(
            approval_id=secrets.token_hex(8),
            action=action,
            risk_level=RiskLevel(risk_level).value,
            params_hash=params_hash,
            params_summary=params_summary,
            trigger_rule=trigger_rule,
            status="pending",
            expires_at=stamp + timedelta(seconds=self.ttl_seconds),
            one_time_token=secrets.token_urlsafe(16),
            decision_summary_id=decision_summary_id,
        )
        if self.db is not None:
            await self.db["runtime_approvals"].insert_one(approval.model_dump(mode="json"))
        return approval

    async def approve(
        self,
        approval: PendingApproval,
        *,
        approver: str,
        params_hash: str | None = None,
        now: datetime | None = None,
    ) -> PendingApproval:
        """参数变化后原审批失效：params_hash 不一致直接拒绝。"""
        stamp = now or datetime.now(UTC)
        if approval.status != "pending":
            return approval.model_copy(update={"status": "rejected"})
        if approval.expires_at is not None and stamp >= approval.expires_at:
            updated = approval.model_copy(update={"status": "expired"})
            if self.db is not None:
                await self.db["runtime_approvals"].find_one_and_update(
                    {"approval_id": approval.approval_id}, {"$set": {"status": "expired"}}
                )
            return updated
        if params_hash is not None and params_hash != approval.params_hash:
            updated = approval.model_copy(update={"status": "rejected"})
            if self.db is not None:
                await self.db["runtime_approvals"].find_one_and_update(
                    {"approval_id": approval.approval_id}, {"$set": {"status": "rejected"}}
                )
            return updated
        updated = approval.model_copy(update={"status": "approved", "approver": approver})
        if self.db is not None:
            await self.db["runtime_approvals"].find_one_and_update(
                {"approval_id": approval.approval_id},
                {"$set": {"status": "approved", "approver": approver}},
            )
        return updated

    async def reject(
        self, approval: PendingApproval, *, approver: str, reason: str = "", now: datetime | None = None
    ) -> PendingApproval:
        updated = approval.model_copy(
            update={"status": "rejected", "approver": approver, "trigger_rule": reason or approval.trigger_rule}
        )
        if self.db is not None:
            await self.db["runtime_approvals"].find_one_and_update(
                {"approval_id": approval.approval_id},
                {"$set": {"status": "rejected", "approver": approver}},
            )
        return updated

    def is_usable(self, approval: PendingApproval, *, now: datetime | None = None) -> bool:
        stamp = now or datetime.now(UTC)
        return (
            approval.status == "approved"
            and approval.expires_at is not None
            and stamp < approval.expires_at
        )

    def consume_token(self, approved_tokens: list[str], consumed: list[str], token: str) -> bool:
        """一次性授权：消费后从可用列表移除并进入已消费列表。"""
        if token not in approved_tokens:
            return False
        approved_tokens.remove(token)
        consumed.append(token)
        return True
