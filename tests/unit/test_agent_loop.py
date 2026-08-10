"""AgentLoop 单元测试 -- 阶段一 Step 5。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.agent_contracts import (
    EventType,
    LoopBudget,
    LoopStatus,
    RunContext,
)
from agent.agent_loop import AgentLoop
from agent.llm_wrapper import LLMWrapper


def _make_ctx(**kw) -> RunContext:
    d = dict(trace_id="t1", run_id="r1", user_id="u1")
    d.update(kw)
    return RunContext(**d)


def _make_budget(**kw) -> LoopBudget:
    d = dict(max_rounds=5, deadline_seconds=30, tool_timeout_seconds=5, max_tool_calls=8)
    d.update(kw)
    return LoopBudget(**d)


def _make_wrapper(response: AIMessage, db=None) -> LLMWrapper:
    llm = MagicMock()
    llm.model_name = "deepseek-chat"
    llm.bind_tools = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock(return_value=response)
    wrapper = LLMWrapper(llm=llm, db=db)
    return wrapper, llm


class TestDirectAnswer:
    """模型不调用工具，直接回答。"""

    @pytest.mark.asyncio
    async def test_direct_answer(self):
        response = AIMessage(content="这是回答。", tool_calls=[])
        wrapper, _ = _make_wrapper(response)
        loop = AgentLoop(
            llm_wrapper=wrapper, tools=[], budget=_make_budget(),
            run_context=_make_ctx(),
        )

        result = await loop.run(system_prompt="sys", user_message="你好")

        assert result.status == LoopStatus.COMPLETED
        assert result.answer == "这是回答。"
        assert result.rounds == 1
        assert not result.degraded
        assert len(result.events) >= 2  # LOOP_START + ROUND_START + ROUND_END + LOOP_END

    @pytest.mark.asyncio
    async def test_events_contain_loop_start(self):
        response = AIMessage(content="ok", tool_calls=[])
        wrapper, _ = _make_wrapper(response)
        loop = AgentLoop(
            llm_wrapper=wrapper, tools=[], budget=_make_budget(),
            run_context=_make_ctx(),
        )

        result = await loop.run(system_prompt="sys", user_message="hi")
        types = [e.type for e in result.events]
        assert EventType.LOOP_START in types
        assert EventType.LOOP_END in types


class TestSingleToolCall:
    """单次工具调用后回答。"""

    @pytest.mark.asyncio
    async def test_single_tool_then_answer(self):
        # 第一次调用：请求工具
        tool_response = AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge", "id": "call_1", "args": {"product_id": "p1"}, "type": "tool_call"}],
        )
        # 第二次调用：最终回答
        answer_response = AIMessage(content="基于知识库回答。", tool_calls=[])

        wrapper, llm = _make_wrapper(tool_response)
        llm.ainvoke = AsyncMock(side_effect=[tool_response, answer_response])

        # mock 工具
        mock_tool = MagicMock()
        mock_tool.name = "search_knowledge"
        mock_tool.ainvoke = AsyncMock(return_value="产品知识内容")

        loop = AgentLoop(
            llm_wrapper=wrapper, tools=[mock_tool], budget=_make_budget(),
            run_context=_make_ctx(allowed_product_ids=frozenset({"p1"})),
        )

        result = await loop.run(system_prompt="sys", user_message="查一下产品")

        assert result.status == LoopStatus.COMPLETED
        assert result.answer == "基于知识库回答。"
        assert result.rounds == 2
        assert "search_knowledge" in result.tool_names_used


class TestMaxRounds:
    """轮次上限。"""

    @pytest.mark.asyncio
    async def test_max_rounds_reached(self):
        # 每次都请求工具，直到 max_rounds
        tool_response = AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge", "id": "call_1", "args": {}, "type": "tool_call"}],
        )
        final_response = AIMessage(content="降级回答。", tool_calls=[])

        wrapper, llm = _make_wrapper(tool_response)
        # 3 次工具调用 + 1 次 finalization
        llm.ainvoke = AsyncMock(side_effect=[tool_response, tool_response, tool_response, final_response])

        mock_tool = MagicMock()
        mock_tool.name = "search_knowledge"
        mock_tool.ainvoke = AsyncMock(return_value="结果")

        loop = AgentLoop(
            llm_wrapper=wrapper, tools=[mock_tool],
            budget=_make_budget(max_rounds=3),
            run_context=_make_ctx(),
        )

        result = await loop.run(system_prompt="sys", user_message="query")

        assert result.degraded
        assert result.rounds >= 3


class TestRepeatedAction:
    """重复动作检测。"""

    @pytest.mark.asyncio
    async def test_repeated_action_blocked(self):
        # 同一工具同一参数调用 3 次（max_repeated=2）
        tool_response = AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge", "id": f"call_{i}", "args": {"product_id": "p1"}, "type": "tool_call"} for i in range(1)],
        )
        answer_response = AIMessage(content="回答。", tool_calls=[])

        wrapper, llm = _make_wrapper(tool_response)
        llm.ainvoke = AsyncMock(side_effect=[tool_response, tool_response, tool_response, answer_response])

        mock_tool = MagicMock()
        mock_tool.name = "search_knowledge"
        mock_tool.ainvoke = AsyncMock(return_value="结果")

        loop = AgentLoop(
            llm_wrapper=wrapper, tools=[mock_tool],
            budget=_make_budget(max_rounds=10),
            run_context=_make_ctx(allowed_product_ids=frozenset({"p1"})),
            max_repeated_actions=2,
        )

        result = await loop.run(system_prompt="sys", user_message="query")

        # 检查事件中是否有 repeated_action
        blocked_events = [e for e in result.events if e.type == EventType.TOOL_BLOCKED]
        assert any(e.error_code == "repeated_action" for e in blocked_events)


class TestToolNotFound:
    """模型调用了不存在的工具。"""

    @pytest.mark.asyncio
    async def test_unknown_tool_blocked(self):
        tool_response = AIMessage(
            content="",
            tool_calls=[{"name": "nonexistent_tool", "id": "call_1", "args": {}, "type": "tool_call"}],
        )
        answer_response = AIMessage(content="回答。", tool_calls=[])

        wrapper, llm = _make_wrapper(tool_response)
        llm.ainvoke = AsyncMock(side_effect=[tool_response, answer_response])

        loop = AgentLoop(
            llm_wrapper=wrapper, tools=[],
            budget=_make_budget(),
            run_context=_make_ctx(),
        )

        result = await loop.run(system_prompt="sys", user_message="query")

        blocked_events = [e for e in result.events if e.type == EventType.TOOL_BLOCKED]
        assert any(e.error_code == "tool_not_found" for e in blocked_events)
        assert result.status == LoopStatus.COMPLETED


class TestToolTimeout:
    """工具执行超时。"""

    @pytest.mark.asyncio
    async def test_tool_timeout(self):
        import asyncio

        tool_response = AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge", "id": "call_1", "args": {}, "type": "tool_call"}],
        )
        answer_response = AIMessage(content="回答。", tool_calls=[])

        wrapper, llm = _make_wrapper(tool_response)
        llm.ainvoke = AsyncMock(side_effect=[tool_response, answer_response])

        mock_tool = MagicMock()
        mock_tool.name = "search_knowledge"

        async def slow_tool(args):
            await asyncio.sleep(10)
            return "result"

        mock_tool.ainvoke = slow_tool

        loop = AgentLoop(
            llm_wrapper=wrapper, tools=[mock_tool],
            budget=_make_budget(tool_timeout_seconds=1),
            run_context=_make_ctx(allowed_product_ids=frozenset({"p1"})),
        )

        result = await loop.run(system_prompt="sys", user_message="query")

        failed_events = [e for e in result.events if e.type == EventType.TOOL_FAILED]
        assert any(e.error_code == "timeout" for e in failed_events)


class TestBudgetExceeded:
    """token 预算超限。"""

    @pytest.mark.asyncio
    async def test_token_budget_exceeded(self):
        tool_response = AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge", "id": "call_1", "args": {}, "type": "tool_call"}],
        )
        final_response = AIMessage(content="降级回答。", tool_calls=[])

        wrapper, llm = _make_wrapper(tool_response)
        llm.ainvoke = AsyncMock(side_effect=[tool_response, final_response])

        mock_tool = MagicMock()
        mock_tool.name = "search_knowledge"
        mock_tool.ainvoke = AsyncMock(return_value="结果")

        loop = AgentLoop(
            llm_wrapper=wrapper, tools=[mock_tool],
            budget=_make_budget(max_input_tokens=1),  # 极小预算
            run_context=_make_ctx(allowed_product_ids=frozenset({"p1"})),
        )

        result = await loop.run(system_prompt="sys", user_message="query")

        assert result.degraded


class TestMessagePairing:
    """每个 tool call 恰好一个同 ID ToolMessage。"""

    @pytest.mark.asyncio
    async def test_tool_message_paired(self):
        tool_response = AIMessage(
            content="",
            tool_calls=[
                {"name": "search_knowledge", "id": "call_a", "args": {}, "type": "tool_call"},
                {"name": "search_knowledge", "id": "call_b", "args": {}, "type": "tool_call"},
            ],
        )
        answer_response = AIMessage(content="回答。", tool_calls=[])

        wrapper, llm = _make_wrapper(tool_response)
        llm.ainvoke = AsyncMock(side_effect=[tool_response, answer_response])

        mock_tool = MagicMock()
        mock_tool.name = "search_knowledge"
        mock_tool.ainvoke = AsyncMock(return_value="结果")

        loop = AgentLoop(
            llm_wrapper=wrapper, tools=[mock_tool],
            budget=_make_budget(),
            run_context=_make_ctx(allowed_product_ids=frozenset({"p1"})),
        )

        result = await loop.run(system_prompt="sys", user_message="query")

        # 检查 messages 中的 ToolMessage 与 tool_call id 配对
        # （通过事件间接验证：2 个 TOOL_FINISHED）
        finished = [e for e in result.events if e.type == EventType.TOOL_FINISHED]
        assert len(finished) == 2
