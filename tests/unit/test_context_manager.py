"""
ContextManager 单元测试（阶段二 Step 4）

覆盖：
  - token 预算推导（0 → 动态窗口）
  - required 不可被挤出、可选按序丢弃
  - 冲突抑制（conflicts）
  - plan hash 稳定
  - 渲染顺序

运行:
    pytest tests/unit/test_context_manager.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.context_manager import (
    ALLOCATION_ORDER,
    ContextManager,
    ContextRequest,
    ContextSource,
    estimate_tokens,
    resolve_model_window,
)


def _source(source: str, content: str, section_type: str, *, required: bool = False, source_hash: str = "h") -> ContextSource:
    return ContextSource(
        source=source,
        content=content,
        section_type=section_type,
        source_hash=source_hash,
        required=required,
    )


# ═══════════════════════════════════════════════════════════════
# token 推导与估算
# ═══════════════════════════════════════════════════════════════


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2


def test_resolve_model_window():
    assert resolve_model_window("deepseek-chat") == 64000
    assert resolve_model_window("deepseek-chat-v3") == 64000  # 前缀匹配
    assert resolve_model_window("unknown-model") == 64000     # 默认


def test_derive_input_budget_dynamic():
    cm = ContextManager()
    req = ContextRequest(
        purpose="score", user_id="u-1", model_id="deepseek-chat",
        max_input_tokens=0,  # 动态
        metadata={"system_tokens": 0},
    )
    budget = cm.derive_input_budget(req)
    # 64000 * 0.9 - (0 + 800 + 4000 + 4000)
    assert budget == int(64000 * 0.9) - 800 - 4000 - 4000


def test_derive_input_budget_explicit():
    cm = ContextManager()
    req = ContextRequest(
        purpose="score", user_id="u-1", max_input_tokens=12000
    )
    assert cm.derive_input_budget(req) == 12000


# ═══════════════════════════════════════════════════════════════
# 分配：required 优先 / optional 丢弃 / 冲突
# ═══════════════════════════════════════════════════════════════


def test_required_allocated_before_optional():
    cm = ContextManager()
    req = ContextRequest(purpose="score", user_id="u-1", max_input_tokens=100000)
    sources = [
        _source("optional:external", "外部资料" * 100, "external"),
        _source("required:overview", "概述知识" * 100, "required_product", required=True),
    ]
    plan = cm.build(req, sources)
    assert plan.total_tokens > 0
    # required 必须存在，且位于可选之前（分配顺序）
    types = [s.source.section_type for s in plan.sections]
    assert "required_product" in types
    assert types.index("required_product") < types.index("external")


def test_optional_dropped_when_budget_exceeded():
    cm = ContextManager()
    req = ContextRequest(purpose="score", user_id="u-1", max_input_tokens=100)
    big_optional = _source("optional:memory", "记忆" * 400, "memory_preference")  # 200 tokens
    plan = cm.build(req, [big_optional])
    assert plan.total_tokens == 0
    assert any(d.source == "optional:memory" and d.reason == "budget_exceeded" for d in plan.dropped)


def test_required_not_evicted_by_optional():
    """required 来源不可被低优先级内容挤出。"""
    cm = ContextManager()
    req = ContextRequest(purpose="score", user_id="u-1", max_input_tokens=1000)
    sources = [
        _source("optional:external", "外部" * 6000, "external"),          # 1500 tokens
        _source("required:overview", "概述" * 2000, "required_product", required=True),  # 500 tokens
    ]
    plan = cm.build(req, sources)
    allocated = {s.source.source for s in plan.sections}
    assert "required:overview" in allocated
    assert "optional:external" not in allocated


def test_required_insufficient_budget_recorded():
    """required 超过总预算时记录 dropped(insufficient)，不静默。"""
    cm = ContextManager()
    req = ContextRequest(purpose="score", user_id="u-1", max_input_tokens=100)
    src = _source("required:policy", "政策" * 400, "security_policy", required=True)  # 200 tokens
    plan = cm.build(req, [src])
    assert plan.total_tokens == 0
    assert any(d.source == "required:policy" and d.reason == "required_insufficient_budget" for d in plan.dropped)


def test_conflict_suppression():
    """同 section_type 的重复来源被抑制并记入 conflicts。"""
    cm = ContextManager()
    req = ContextRequest(purpose="score", user_id="u-1", max_input_tokens=100000)
    sources = [
        _source("policy:base", "基础政策" * 10, "security_policy"),
        _source("policy:extra", "额外政策" * 10, "security_policy"),
    ]
    plan = cm.build(req, sources)
    # 第一个分配，第二个被抑制
    assert len(plan.sections) == 1
    assert any(c.suppressed_by == "policy:base" and c.source == "policy:extra" for c in plan.conflicts)


def test_allocation_order():
    assert ALLOCATION_ORDER[0] == "security_policy"
    assert ALLOCATION_ORDER[-1] == "external"
    # 用户约束高于 Skill 核心
    assert ALLOCATION_ORDER.index("user_constraints") < ALLOCATION_ORDER.index("skill_core")
    # required 产品知识高于用户知识
    assert ALLOCATION_ORDER.index("required_product") < ALLOCATION_ORDER.index("user_knowledge")


# ═══════════════════════════════════════════════════════════════
# plan hash / rendered
# ═══════════════════════════════════════════════════════════════


def test_plan_hash_stable_and_sensitive():
    cm = ContextManager()
    req = ContextRequest(purpose="score", user_id="u-1", max_input_tokens=100000)
    sources = [
        _source("required:overview", "概述" * 10, "required_product", required=True, source_hash="h1"),
    ]
    plan1 = cm.build(req, sources, snapshot={"skill_versions": "v1", "knowledge_snapshot": "k1"})
    plan2 = cm.build(req, sources, snapshot={"skill_versions": "v1", "knowledge_snapshot": "k1"})
    assert plan1.plan_hash == plan2.plan_hash

    # 内容变化 → hash 变化
    plan3 = cm.build(req, [
        _source("required:overview", "概述不同" * 10, "required_product", required=True, source_hash="h2"),
    ], snapshot={"skill_versions": "v1", "knowledge_snapshot": "k1"})
    assert plan1.plan_hash != plan3.plan_hash


def test_rendered_joins_sections():
    cm = ContextManager()
    req = ContextRequest(purpose="score", user_id="u-1", max_input_tokens=100000)
    sources = [
        _source("a", "AAA", "security_policy"),
        _source("b", "BBB", "external"),
    ]
    plan = cm.build(req, sources)
    assert plan.rendered() == "AAA\n\nBBB"
