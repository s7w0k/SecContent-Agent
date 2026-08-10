"""Agent 公共契约单元测试 -- 阶段一 Step 2。

验证 agent_contracts.py 中所有数据结构的边界行为：
  - RunContext 权限检查与过期判定
  - LoopBudget / BudgetUsage 预算耗尽判定
  - TypedToolResult 成功/失败构造与消息转换
  - LoopStatus / LoopResult 状态语义
  - AgentEvent 脱敏日志输出
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from agent.agent_contracts import (
    AgentEvent,
    BudgetUsage,
    EventType,
    LoopBudget,
    LoopResult,
    LoopStatus,
    RunContext,
    ToolPolicy,
    TypedToolResult,
)

# ═══════════════════════════════════════════════════════════════
# RunContext
# ═══════════════════════════════════════════════════════════════


class TestRunContext:
    """RunContext 权限与过期测试。"""

    def test_frozen(self):
        """RunContext 不可变。"""
        ctx = RunContext(trace_id="t1", run_id="r1", user_id="u1")
        with pytest.raises(AttributeError):
            ctx.user_id = "u2"

    def test_article_allowlist(self):
        ctx = RunContext(
            trace_id="t1",
            run_id="r1",
            user_id="u1",
            allowed_article_hashes=frozenset({"hash_a", "hash_b"}),
        )
        assert ctx.is_article_allowed("hash_a")
        assert ctx.is_article_allowed("hash_b")
        assert not ctx.is_article_allowed("hash_c")

    def test_article_empty_allowlist_denies_all(self):
        ctx = RunContext(trace_id="t1", run_id="r1", user_id="u1")
        assert not ctx.is_article_allowed("any")

    def test_product_allowlist(self):
        ctx = RunContext(
            trace_id="t1",
            run_id="r1",
            user_id="u1",
            allowed_product_ids=frozenset({"p1", "p2"}),
        )
        assert ctx.is_product_allowed("p1")
        assert not ctx.is_product_allowed("p3")

    def test_expired(self):
        past = datetime.now(UTC) - timedelta(seconds=1)
        ctx = RunContext(trace_id="t1", run_id="r1", user_id="u1", deadline_at=past)
        assert ctx.is_expired()

    def test_not_expired(self):
        future = datetime.now(UTC) + timedelta(seconds=30)
        ctx = RunContext(trace_id="t1", run_id="r1", user_id="u1", deadline_at=future)
        assert not ctx.is_expired()

    def test_no_deadline_never_expires(self):
        ctx = RunContext(trace_id="t1", run_id="r1", user_id="u1")
        assert not ctx.is_expired()

    def test_tenant_id_optional_and_default_none(self):
        ctx = RunContext(trace_id="t1", run_id="r1", user_id="u1")
        assert ctx.tenant_id is None


# ═══════════════════════════════════════════════════════════════
# Budget
# ═══════════════════════════════════════════════════════════════


class TestLoopBudget:
    def test_defaults(self):
        b = LoopBudget()
        assert b.max_rounds == 5
        assert b.max_tool_calls == 8
        assert b.max_cost_usd == 0.0

    def test_frozen(self):
        b = LoopBudget()
        with pytest.raises(AttributeError):
            b.max_rounds = 10


class TestBudgetUsage:
    def test_initial_can_continue(self):
        usage = BudgetUsage()
        budget = LoopBudget()
        assert usage.can_continue(budget)

    def test_rounds_exceeded(self):
        usage = BudgetUsage(rounds=5)
        budget = LoopBudget(max_rounds=5)
        assert not usage.can_continue(budget)

    def test_input_tokens_exceeded(self):
        usage = BudgetUsage(input_tokens=24000)
        budget = LoopBudget(max_input_tokens=24000)
        assert not usage.can_continue(budget)

    def test_tool_calls_exceeded(self):
        usage = BudgetUsage(tool_calls=8)
        budget = LoopBudget(max_tool_calls=8)
        assert not usage.can_continue(budget)

    def test_deadline_exceeded(self):
        usage = BudgetUsage(started_at=datetime.now(UTC) - timedelta(seconds=31))
        budget = LoopBudget(deadline_seconds=30)
        assert not usage.can_continue(budget)

    def test_cost_exceeded(self):
        usage = BudgetUsage(cost_usd=0.50)
        budget = LoopBudget(max_cost_usd=0.50)
        assert not usage.can_continue(budget)

    def test_cost_zero_means_unlimited(self):
        usage = BudgetUsage(cost_usd=999.0)
        budget = LoopBudget(max_cost_usd=0.0)
        assert usage.can_continue(budget)

    def test_total_tokens(self):
        usage = BudgetUsage(input_tokens=100, output_tokens=50)
        assert usage.total_tokens == 150


# ═══════════════════════════════════════════════════════════════
# TypedToolResult
# ═══════════════════════════════════════════════════════════════


class TestTypedToolResult:
    def test_success(self):
        r = TypedToolResult.success("knowledge content", source_ids=["doc1", "doc2"])
        assert r.ok
        assert r.data == "knowledge content"
        assert r.source_ids == ["doc1", "doc2"]
        assert not r.truncated

    def test_success_truncated(self):
        r = TypedToolResult.success("long...", truncated=True, char_count=5000)
        assert r.truncated
        assert r.char_count == 5000

    def test_failure(self):
        r = TypedToolResult.failure("DB unavailable", error_code="db_unavailable")
        assert not r.ok
        assert r.error == "DB unavailable"
        assert r.error_code == "db_unavailable"

    def test_to_tool_message_content_success(self):
        r = TypedToolResult.success("data here")
        assert r.to_tool_message_content() == "data here"

    def test_to_tool_message_content_truncated(self):
        r = TypedToolResult.success("data", truncated=True)
        assert "(结果已截断)" in r.to_tool_message_content()

    def test_to_tool_message_content_failure(self):
        r = TypedToolResult.failure("timeout", error_code="timeout")
        content = r.to_tool_message_content()
        assert "[工具执行失败]" in content
        assert "timeout" in content
        assert "error_code=timeout" in content

    def test_frozen(self):
        r = TypedToolResult.success("data")
        with pytest.raises(AttributeError):
            r.data = "modified"


# ═══════════════════════════════════════════════════════════════
# ToolPolicy
# ═══════════════════════════════════════════════════════════════


class TestToolPolicy:
    def test_defaults(self):
        p = ToolPolicy(name="search_knowledge")
        assert p.idempotent
        assert p.timeout_seconds == 5

    def test_custom(self):
        p = ToolPolicy(
            name="fetch_fulltext",
            idempotent=False,
            timeout_seconds=10,
            requires_article_allowlist=True,
        )
        assert not p.idempotent
        assert p.timeout_seconds == 10
        assert p.requires_article_allowlist

    def test_frozen(self):
        p = ToolPolicy(name="test")
        with pytest.raises(AttributeError):
            p.name = "other"


# ═══════════════════════════════════════════════════════════════
# LoopResult
# ═══════════════════════════════════════════════════════════════


class TestLoopResult:
    def test_completed_ok(self):
        r = LoopResult(status=LoopStatus.COMPLETED, answer="answer", rounds=2)
        assert r.ok
        assert not r.degraded

    def test_degraded_not_ok(self):
        r = LoopResult(status=LoopStatus.DEGRADED, answer="fallback", degraded=True)
        assert not r.ok
        assert r.degraded

    def test_failed_not_ok(self):
        r = LoopResult(status=LoopStatus.FAILED)
        assert not r.ok

    def test_cancelled_not_ok(self):
        r = LoopResult(status=LoopStatus.CANCELLED)
        assert not r.ok

    def test_defaults(self):
        r = LoopResult(status=LoopStatus.RUNNING)
        assert r.answer == ""
        assert r.rounds == 0
        assert r.references == []
        assert r.events == []
        assert r.tool_names_used == []


# ═══════════════════════════════════════════════════════════════
# AgentEvent
# ═══════════════════════════════════════════════════════════════


class TestAgentEvent:
    def test_to_log_dict_has_required_fields(self):
        e = AgentEvent(
            type=EventType.TOOL_STARTED,
            sequence=1,
            run_id="r1",
            trace_id="t1",
            tool_name="search_knowledge",
            tool_args_hash="sha256:abc",
            round_no=0,
        )
        d = e.to_log_dict()
        assert d["type"] == "tool_started"
        assert d["sequence"] == 1
        assert d["run_id"] == "r1"
        assert d["trace_id"] == "t1"
        assert d["tool_name"] == "search_knowledge"
        assert d["tool_args_hash"] == "sha256:abc"
        assert d["round_no"] == 0
        assert "timestamp" in d

    def test_to_log_dict_excludes_sensitive_data(self):
        """日志字典不应包含完整 args/result/prompt。"""
        e = AgentEvent(
            type=EventType.TOOL_FINISHED,
            sequence=2,
            run_id="r1",
            trace_id="t1",
            tool_name="get_article",
            tool_args_hash="sha256:xyz",
            tool_result_hash="sha256:result",
        )
        d = e.to_log_dict()
        # 只有 hash，没有原始内容
        assert "tool_args" not in d
        assert "tool_result" not in d
        assert "prompt" not in d
        assert d["tool_args_hash"] == "sha256:xyz"
        assert d["tool_result_hash"] == "sha256:result"

    def test_frozen(self):
        e = AgentEvent(type=EventType.LOOP_START, sequence=0, run_id="r1", trace_id="t1")
        with pytest.raises(AttributeError):
            e.type = EventType.LOOP_END


# ═══════════════════════════════════════════════════════════════
# Config 默认值验证
# ═══════════════════════════════════════════════════════════════


class TestConfigDefaults:
    """验证 Agent Loop 配置默认值（flag 全部关闭）。"""

    def test_all_flags_disabled_by_default(self):
        # 不从 .env 读取，用空环境
        import os

        from config import Settings
        old_environ = dict(os.environ)
        try:
            # 清除可能影响的环境变量
            for key in [
                "CHAT_AGENT_ENABLED", "CHAT_ASK_AGENT_ENABLED",
                "CHAT_REVISE_AGENT_ENABLED", "CHAT_AGENT_SHADOW_ENABLED",
            ]:
                os.environ.pop(key, None)
            s = Settings()
            assert s.CHAT_AGENT_ENABLED is False
            assert s.CHAT_ASK_AGENT_ENABLED is False
            assert s.CHAT_REVISE_AGENT_ENABLED is False
            assert s.CHAT_AGENT_SHADOW_ENABLED is False
            assert s.CHAT_AGENT_ROLLOUT_PERCENT == 0
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

    def test_budget_values(self):
        from config import Settings

        s = Settings()
        assert s.CHAT_AGENT_MAX_ROUNDS == 5
        assert s.CHAT_AGENT_MAX_INPUT_TOKENS == 24000
        assert s.CHAT_AGENT_MAX_OUTPUT_TOKENS == 4000
        assert s.CHAT_AGENT_MAX_TOOL_CALLS == 8
        assert s.CHAT_AGENT_MAX_PARALLEL_TOOLS == 3
        assert s.CHAT_AGENT_DEADLINE_SECONDS == 30
        assert s.CHAT_AGENT_TOOL_TIMEOUT_SECONDS == 5
        assert s.CHAT_AGENT_EVENT_TTL_DAYS == 30
        assert s.CHAT_AGENT_MAX_COST_USD == 0.0
        assert s.CHAT_SSE_SCHEMA_VERSION == "1.0"

    def test_budget_from_settings(self):
        """从 settings 构建 LoopBudget。"""
        from config import Settings

        s = Settings()
        budget = LoopBudget(
            max_rounds=s.CHAT_AGENT_MAX_ROUNDS,
            max_input_tokens=s.CHAT_AGENT_MAX_INPUT_TOKENS,
            max_output_tokens=s.CHAT_AGENT_MAX_OUTPUT_TOKENS,
            max_tool_calls=s.CHAT_AGENT_MAX_TOOL_CALLS,
            max_parallel_tools=s.CHAT_AGENT_MAX_PARALLEL_TOOLS,
            deadline_seconds=s.CHAT_AGENT_DEADLINE_SECONDS,
            tool_timeout_seconds=s.CHAT_AGENT_TOOL_TIMEOUT_SECONDS,
            max_cost_usd=s.CHAT_AGENT_MAX_COST_USD,
        )
        assert budget.max_rounds == 5
        assert budget.max_tool_calls == 8
