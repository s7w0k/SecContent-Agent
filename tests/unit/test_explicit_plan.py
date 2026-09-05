"""P1 显式 Plan（形态 A）测试：确定性计划构建、白名单清洗、AgentEngine 事件集成。"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, "services/backend")

from agent.agent_engine import AgentEngine
from agent.business_tools.contracts import build_business_tool_registry
from agent.plan_explicit import (
    PR_TOOL_ORDER,
    ExplicitPlan,
    LlmPlanOutput,
    LlmPlanStep,
    build_deterministic_plan,
    build_llm_plan,
    sanitize_plan,
)
from langchain_core.messages import AIMessage


def _registry():
    return build_business_tool_registry()


def _engine(**overrides):
    registry = _registry()
    events: list[tuple[int, str, dict]] = []

    async def sink(sequence: int, event_type: str, payload: dict) -> None:
        events.append((sequence, event_type, payload))

    engine = AgentEngine(
        llm_wrapper=SimpleNamespace(llm=SimpleNamespace(bind_tools=lambda _tools: None)),
        executor=AsyncMock(),
        registry=registry,
        tool_ctx=SimpleNamespace(user_id="u1", tenant_id="t1"),
        adapter="fake",
        run_context=SimpleNamespace(run_id="run-1"),
        event_sink=sink,
        **overrides,
    )
    return engine, events


# ── 确定性计划构建 ─────────────────────────────────────


def test_deterministic_plan_follows_pr_order_within_budget():
    allowed = set(PR_TOOL_ORDER)
    plan = build_deterministic_plan(allowed, max_steps=6, run_id="run-x")
    assert isinstance(plan, ExplicitPlan)
    flat_tools = [tool for step in plan.steps for tool in step.tools]
    # 白名单内每个工具都应出现在计划中（顺序按惯例、不丢失）
    assert set(flat_tools) == allowed
    # 出现顺序与 PR_TOOL_ORDER 一致
    ordered = [t for t in PR_TOOL_ORDER if t in flat_tools]
    flat_by_order = sorted(flat_tools, key=lambda t: ordered.index(t))
    assert flat_tools == flat_by_order
    assert len(plan.steps) <= 6


def test_deterministic_plan_merges_tail_when_over_budget():
    allowed = set(PR_TOOL_ORDER)
    plan = build_deterministic_plan(allowed, max_steps=3)
    assert len(plan.steps) <= 3
    flat = [tool for step in plan.steps for tool in step.tools]
    assert set(flat) == allowed  # 合并到通用步骤后仍不丢工具


def test_deterministic_plan_appends_unknown_tools_and_fallback_when_empty():
    plan = build_deterministic_plan({"custom_tool_x", "get_article"}, max_steps=6)
    flat = [tool for step in plan.steps for tool in step.tools]
    assert "custom_tool_x" in flat

    fallback = build_deterministic_plan(set(), max_steps=6)
    assert len(fallback.steps) == 1
    assert fallback.steps[0].tools == []


# ── 白名单清洗 ────────────────────────────────────────


def test_sanitize_plan_filters_unknown_tools_and_empty_steps():
    raw = {
        "steps": [
            {"step_id": "s1", "tools": ["get_article", "evil_tool"], "title": "x"},
            {"step_id": "s2", "tools": ["not_allowed"]},
            {"step_id": "s3", "tools": ["score_article"], "expected_output": "score"},
        ]
    }
    plan = sanitize_plan(raw, allowed_tools={"get_article", "score_article"}, max_steps=6)
    assert plan is not None
    assert plan.steps[0].tools == ["get_article"]
    assert [s.step_id for s in plan.steps] == ["s1", "s3"]


def test_sanitize_plan_caps_steps_and_rejects_garbage():
    raw = {"steps": [{"tools": [f"t{i}"]} for i in range(20)]}
    plan = sanitize_plan(raw, allowed_tools={f"t{i}" for i in range(20)}, max_steps=3)
    assert plan is not None and len(plan.steps) == 3
    assert sanitize_plan(None, allowed_tools={"a"}) is None
    assert sanitize_plan({"steps": []}, allowed_tools={"a"}) is None
    assert sanitize_plan({"steps": [{"tools": ["unknown"]}]}, allowed_tools={"a"}) is None


def test_plan_fingerprint_is_stable():
    allowed = set(PR_TOOL_ORDER)
    one = build_deterministic_plan(allowed, run_id="r1")
    two = build_deterministic_plan(allowed, run_id="r2")
    assert one.fingerprint() == two.fingerprint()  # run_id 不影响计划指纹


# ── LLM 驱动规划 ─────────────────────────────────────


class _FakeLlmWrapper:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    async def invoke_structured(self, *args, **kwargs):
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_llm_plan_keeps_allowed_tools_and_caps_steps():
    wrapper = _FakeLlmWrapper(
        result=LlmPlanOutput(
            steps=[
                LlmPlanStep(title="读文", tools=["get_article"], expected_output="article"),
                LlmPlanStep(
                    title="坏工具", tools=["evil_tool", "score_article"], expected_output=""
                ),
                LlmPlanStep(title="写稿", tools=["generate_draft"]),
            ]
        )
    )
    steps = await build_llm_plan(
        wrapper,
        goal="写稿",
        allowed_tools={"get_article", "score_article", "generate_draft"},
        max_steps=2,
    )
    assert steps is not None
    assert len(steps) <= 2  # 超限截断
    assert all(set(s["tools"]) <= {"get_article", "score_article", "generate_draft"} for s in steps)
    assert all(s["tools"] for s in steps)  # 白名单外的坏工具步骤被丢弃


@pytest.mark.asyncio
async def test_llm_plan_returns_none_on_empty_or_error():
    empty = await build_llm_plan(
        _FakeLlmWrapper(result=LlmPlanOutput(steps=[])),
        goal="hi",
        allowed_tools={"get_article"},
    )
    assert empty is None
    broken = await build_llm_plan(
        _FakeLlmWrapper(error=RuntimeError("llm down")),
        goal="hi",
        allowed_tools={"get_article"},
    )
    assert broken is None


# ── AgentEngine 事件集成 ───────────────────────────────


@pytest.mark.asyncio
async def test_engine_emits_plan_before_loop_when_planner_provided():
    engine, events = _engine(
        explicit_planner=AsyncMock(
            return_value={"steps": [{"step_id": "s1", "tools": ["generate_draft"]}]}
        )
    )
    # 让引擎第一轮就给出最终文本（无工具调用）
    engine._invoke = AsyncMock(return_value=AIMessage(content="完成", tool_calls=[]))  # type: ignore[assignment]
    await engine.run(system_prompt="sys", history=[], user_message="生成一篇 PR 稿")

    types = [event_type for _, event_type, _ in events]
    assert "plan" in types
    assert engine.explicit_planner.await_args.args[0] == "生成一篇 PR 稿"  # type: ignore[union-attr]
    plan_event = next(payload for _, et, payload in events if et == "plan")
    assert plan_event["run_id"] == "run-1"
    steps = plan_event["steps"]
    assert steps and steps[0]["tools"] == ["generate_draft"]
    assert steps[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_engine_does_not_emit_plan_by_default():
    engine, events = _engine()
    engine._invoke = AsyncMock(return_value=AIMessage(content="完成", tool_calls=[]))  # type: ignore[assignment]
    await engine.run(system_prompt="sys", history=[], user_message="hi")
    assert all(event_type != "plan" for _, event_type, _ in events)


@pytest.mark.asyncio
async def test_engine_skips_plan_when_resuming_from_snapshot():
    planner = AsyncMock(return_value={"steps": [{"step_id": "s1", "tools": ["generate_draft"]}]})
    engine, events = _engine(explicit_planner=planner)
    engine._invoke = AsyncMock(return_value=AIMessage(content="继续", tool_calls=[]))  # type: ignore[assignment]
    await engine.run(
        system_prompt="sys",
        history=[],
        user_message="继续",
        initial_messages=[
            {"role": "assistant", "content": "先前快照"},
        ],
    )
    planner.assert_not_awaited()
    assert all(event_type != "plan" for _, event_type, _ in events)


# ── 运行预算护栏（P2） ─────────────────────────────────


@pytest.mark.asyncio
async def test_engine_stops_when_tool_call_budget_exceeded():
    """单次 run 超过 max_tool_calls 即停止为 budget_exceeded，不再进入下一轮。"""
    engine, events = _engine(max_tool_calls=1)
    engine._invoke = AsyncMock(  # type: ignore[assignment]
        side_effect=[
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "generate_draft", "args": {}}],
            ),
            AIMessage(content="完成", tool_calls=[]),
        ]
    )
    result = await engine.run(system_prompt="sys", history=[], user_message="生成 PR 稿")

    assert result["status"] == "budget_exceeded"
    assert result["final_text"] == ""
    assert any(event_type == "budget_exceeded" for _, event_type, _ in events)


@pytest.mark.asyncio
async def test_engine_completes_without_budget_limit_by_default():
    engine, events = _engine()
    engine._invoke = AsyncMock(return_value=AIMessage(content="完成", tool_calls=[]))  # type: ignore[assignment]
    result = await engine.run(system_prompt="sys", history=[], user_message="hi")
    assert result["status"] == "completed"
    assert all(event_type != "budget_exceeded" for _, event_type, _ in events)
