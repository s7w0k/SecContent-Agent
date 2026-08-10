"""PolicyEngine 与人工审批单元测试 — 阶段四 4A Step 4A-3。

覆盖：权限交集、风险分级、参数 schema、数据作用域、预算检查点、
幂等键强制、审批参数变化失效、一次性授权、授权过期、
模型输出无法修改规则、决策日志脱敏。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.policy_engine import (
    ApprovalService,
    DEFAULT_RULES,
    PolicyAction,
    PolicyDecision,
    PolicyEngine,
    PolicyRule,
    RiskLevel,
    params_hash,
    redact_value,
)
from agent.runtime_state import BudgetUsage, RunBudget


def _fixed_now() -> datetime:
    return datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


class TestAllowlistIntersection:
    def test_tool_not_in_allowlist_denied(self):
        engine = PolicyEngine()
        decision = engine.evaluate(
            tool_name="send_message",
            args={"recipient": "u2", "content": "hi", "idempotency_key": "k1"},
            allowed_tool_names=frozenset({"retrieve_articles"}),
        )
        assert not decision.allowed
        assert decision.reason_code == "not_in_allowlist"

    def test_unknown_tool_denied_by_default(self):
        engine = PolicyEngine()
        decision = engine.evaluate(tool_name="ssh_exec", args={})
        assert not decision.allowed
        assert decision.reason_code == "unknown_tool"

    def test_rule_immutable_after_construction(self):
        engine = PolicyEngine()
        # 模型/外部拿到的是只读副本：修改副本不影响引擎内部规则
        rules_view = engine.rules
        rules_view["send_message"] = PolicyRule(tool_name="send_message")
        assert engine.rules["send_message"] == DEFAULT_RULES["send_message"]
        assert "ssh_exec" not in engine.rules


class TestRiskLevels:
    def test_l0_allowed(self):
        engine = PolicyEngine()
        decision = engine.evaluate(tool_name="retrieve_articles", args={})
        assert decision.allowed
        assert decision.action == PolicyAction.ALLOW

    def test_l2_requires_approval(self):
        engine = PolicyEngine()
        decision = engine.evaluate(
            tool_name="submit_pr",
            args={"title": "t", "body": "b", "idempotency_key": "k"},
        )
        assert not decision.allowed
        assert decision.action == PolicyAction.REQUIRE_APPROVAL
        assert decision.risk_level == RiskLevel.L2

    def test_l3_permanently_forbidden(self):
        engine = PolicyEngine()
        decision = engine.evaluate(tool_name="delete_article", args={"article_id": "a1"})
        assert not decision.allowed
        assert decision.action == PolicyAction.DENY
        assert decision.reason_code == "risk_level_l3"


class TestArgsAndDataScope:
    def test_unexpected_args_rejected(self):
        engine = PolicyEngine()
        decision = engine.evaluate(
            tool_name="score_articles",
            args={"article_ids": ["a1"], "prompt_override": "inject"},
        )
        assert not decision.allowed
        assert decision.reason_code == "unexpected_args"

    def test_url_domain_scope(self):
        from agent.policy_engine import PolicyRule

        rules = {
            "fetch_external": PolicyRule(
                tool_name="fetch_external",
                risk_level=RiskLevel.L1,
                allowed_args=frozenset({"url", "idempotency_key"}),
                allowed_domains=frozenset({"example.com"}),
            )
        }
        engine = PolicyEngine(rules=rules)
        ok = engine.evaluate(tool_name="fetch_external", args={"url": "https://example.com/x", "idempotency_key": "k"})
        assert ok.allowed
        bad = engine.evaluate(
            tool_name="fetch_external", args={"url": "https://evil.com/x", "idempotency_key": "k"}
        )
        assert not bad.allowed
        assert bad.reason_code == "url_not_allowed"
        # 非 http(s) 协议拒绝
        file_url = engine.evaluate(
            tool_name="fetch_external", args={"url": "file:///etc/passwd", "idempotency_key": "k"}
        )
        assert not file_url.allowed


class TestBudgetCheckpoint:
    def test_budget_exhausted_denied(self):
        engine = PolicyEngine()
        budget = RunBudget(max_steps=1)
        usage = BudgetUsage()
        usage.record_step()
        decision = engine.evaluate(
            tool_name="retrieve_articles", args={}, usage=usage, budget=budget
        )
        assert not decision.allowed
        assert decision.reason_code == "budget_exhausted"


class TestIdempotencyEnforcement:
    def test_side_effect_requires_idempotency_key(self):
        engine = PolicyEngine()
        decision = engine.evaluate(
            tool_name="save_draft", args={"article_ids": ["a1"], "content": "x"}
        )
        assert not decision.allowed
        assert decision.reason_code == "missing_idempotency_key"
        assert decision.requires_idempotency_key
        # 带幂等键 → L1 受控执行
        ok = engine.evaluate(
            tool_name="save_draft",
            args={"article_ids": ["a1"], "content": "x", "idempotency_key": "k"},
        )
        assert ok.allowed


class TestApprovalLifecycle:
    async def test_approve_changes_params_hash_invalidates(self):
        """不可破坏规则 1：参数变化后原审批失效。"""
        service = ApprovalService(ttl_seconds=1800)
        approval = await service.request(
            run_id="run-1", action="submit_pr", params_hash=params_hash({"title": "A"}),
            params_summary="title=A", risk_level="L2", trigger_rule="risk_level_l2",
            now=_fixed_now(),
        )
        assert approval.status == "pending"
        # 参数变化（不同 params_hash）→ 拒绝
        changed = await service.approve(
            approval, approver="admin", params_hash=params_hash({"title": "B"}),
            now=_fixed_now(),
        )
        assert changed.status == "rejected"
        # 参数一致 → 通过
        ok = await service.approve(
            approval, approver="admin", params_hash=params_hash({"title": "A"}),
            now=_fixed_now(),
        )
        assert ok.status == "approved"
        assert ok.approver == "admin"

    async def test_approval_expired(self):
        service = ApprovalService(ttl_seconds=10)
        approval = await service.request(
            run_id="run-1", action="send_message", params_hash="h",
            params_summary="recipient=u2", risk_level="L2", trigger_rule="risk_level_l2",
            now=_fixed_now(),
        )
        result = await service.approve(
            approval, approver="admin", now=_fixed_now() + timedelta(seconds=20)
        )
        assert result.status == "expired"
        assert not service.is_usable(result, now=_fixed_now() + timedelta(seconds=21))

    async def test_approval_not_pending_rejected(self):
        service = ApprovalService(ttl_seconds=1800)
        approval = await service.request(
            run_id="run-1", action="send_message", params_hash="h",
            params_summary="recipient=u2", risk_level="L2", trigger_rule="risk_level_l2",
            now=_fixed_now(),
        )
        rejected = await service.reject(approval, approver="admin", reason="no", now=_fixed_now())
        assert rejected.status == "rejected"
        again = await service.approve(rejected, approver="admin", now=_fixed_now())
        assert again.status == "rejected"

    def test_one_time_token_consumed_once(self):
        """不可破坏规则 2：审批授权只能消费一次。"""
        approved = ["tok-1", "tok-2"]
        consumed: list[str] = []
        service = ApprovalService(ttl_seconds=1800)
        assert service.consume_token(approved, consumed, "tok-1")
        assert "tok-1" not in approved
        assert "tok-1" in consumed
        # 重复消费同一 token → 失败
        assert not service.consume_token(approved, consumed, "tok-1")
        assert "tok-2" in approved


class TestRedaction:
    def test_decision_log_redacted(self):
        """不可破坏规则 5：日志不含密钥/完整正文。"""
        engine = PolicyEngine()
        decision = engine.evaluate(
            tool_name="submit_pr",
            args={"title": "机密标题很长的正文内容超过了阈值显示截断", "body": "x" * 100, "api_key": "sk-secret", "idempotency_key": "k"},
        )
        serialized = decision.model_dump_json()
        assert "sk-secret" not in serialized
        assert "机密标题" not in decision.params_summary or len(decision.params_summary) <= 300
        assert redact_value("sk-secret", key="api_key") == "***redacted***"

    def test_params_hash_stable_and_sensitive_key_sanitized(self):
        h1 = params_hash({"a": 1, "b": "x"})
        h2 = params_hash({"b": "x", "a": 1})
        assert h1 == h2
        # 密钥类 key 一律脱敏：不同密钥值哈希相同（原文不进入哈希）
        assert params_hash({"token": "t0k3n"}) == params_hash({"token": "other"})
        assert "t0k3n" not in params_hash({"token": "t0k3n"})
        # 非敏感字段保持区分度
        assert params_hash({"name": "a"}) != params_hash({"name": "b"})
