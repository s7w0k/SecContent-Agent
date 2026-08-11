"""AgentLoop 状态机（v2）单元测试 -- 阶段1 1.1/1.2/1.3/2/3 节。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.agent_contracts import LoopBudget, LoopStatus, RunContext
from agent.agent_event_store import COLLECTION, AgentEventStore
from agent.agent_loop import AgentLoop
from agent.budget_manager import BudgetPlan
from agent.context_optimizer import ToolResultCache
from agent.llm_wrapper import LLMWrapper
from langchain_core.messages import AIMessage


def _make_ctx(**kw) -> RunContext:
    d = {"trace_id": "t1", "run_id": "r1", "user_id": "u1"}
    d.update(kw)
    return RunContext(**d)


def _make_budget(**kw) -> LoopBudget:
    d = {"max_rounds": 5, "deadline_seconds": 30, "tool_timeout_seconds": 5, "max_tool_calls": 8}
    d.update(kw)
    return LoopBudget(**d)


def _make_wrapper(response: AIMessage, db=None) -> tuple[LLMWrapper, MagicMock]:
    llm = MagicMock()
    llm.model_name = "deepseek-chat"
    llm.bind_tools = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock(return_value=response)
    wrapper = LLMWrapper(llm=llm, db=db)
    return wrapper, llm


def _tool_call(name: str, call_id: str, args=None) -> dict:
    return {"name": name, "id": call_id, "args": args or {}, "type": "tool_call"}


class _Tool:
    def __init__(self, name: str, result: str = "结果"):
        self.name = name
        self.result = result
        self.calls = 0

    async def ainvoke(self, args):
        self.calls += 1
        return self.result


class TestMultiToolConcurrent:
    """一轮多工具真实并发并回填。"""

    @pytest.mark.asyncio
    async def test_two_tools_executed(self):
        tool_response = AIMessage(
            content="",
            tool_calls=[
                _tool_call("search_knowledge", "c1", {"product_id": "p1"}),
                _tool_call("get_article", "c2", {"url_hash": "h1"}),
            ],
        )
        answer_response = AIMessage(content="综合回答。", tool_calls=[])
        wrapper, llm = _make_wrapper(tool_response)
        llm.ainvoke = AsyncMock(side_effect=[tool_response, answer_response])

        t1 = _Tool("search_knowledge")
        t2 = _Tool("get_article")
        loop = AgentLoop(
            llm_wrapper=wrapper,
            tools=[t1, t2],
            budget=_make_budget(),
            run_context=_make_ctx(),
        )

        result = await loop.run(system_prompt="sys", user_message="q")
        assert result.status == LoopStatus.COMPLETED
        assert result.answer == "综合回答。"
        assert set(result.tool_names_used) == {"search_knowledge", "get_article"}
        assert t1.calls == 1 and t2.calls == 1


class TestToolResultCacheHit:
    """工具结果缓存命中：不执行工具直接复用。"""

    @pytest.mark.asyncio
    async def test_cache_hit_skips_execution(self):
        cache = ToolResultCache()
        cache.set(
            user_id="u1",
            tool_name="search_knowledge",
            args={"product_id": "p1"},
            content="缓存内容",
            source_ids=["s9"],
            result_hash="hx",
        )
        tool_response = AIMessage(
            content="",
            tool_calls=[_tool_call("search_knowledge", "c1", {"product_id": "p1"})],
        )
        answer_response = AIMessage(content="基于缓存回答。", tool_calls=[])
        wrapper, llm = _make_wrapper(tool_response)
        llm.ainvoke = AsyncMock(side_effect=[tool_response, answer_response])

        tool = _Tool("search_knowledge")
        loop = AgentLoop(
            llm_wrapper=wrapper,
            tools=[tool],
            budget=_make_budget(),
            run_context=_make_ctx(),
            tool_result_cache=cache,
        )

        result = await loop.run(system_prompt="sys", user_message="q")
        assert result.status == LoopStatus.COMPLETED
        assert tool.calls == 0  # 缓存命中，工具未执行
        assert "s9" in result.references


class TestValidateEmptyAnswer:
    """模型未返回工具且输出为空：首次 replan，二次降级。"""

    @pytest.mark.asyncio
    async def test_empty_answer_then_replan_success(self):
        empty = AIMessage(content="", tool_calls=[])
        ok_answer = AIMessage(content="最终回答。", tool_calls=[])
        wrapper, llm = _make_wrapper(empty)
        llm.ainvoke = AsyncMock(side_effect=[empty, ok_answer])

        loop = AgentLoop(
            llm_wrapper=wrapper,
            tools=[],
            budget=_make_budget(),
            run_context=_make_ctx(),
        )
        result = await loop.run(system_prompt="sys", user_message="q")
        assert result.status == LoopStatus.COMPLETED
        assert result.answer == "最终回答。"

    @pytest.mark.asyncio
    async def test_empty_answer_twice_degrades(self):
        empty = AIMessage(content="", tool_calls=[])
        wrapper, llm = _make_wrapper(empty)
        llm.ainvoke = AsyncMock(side_effect=[empty, empty])

        loop = AgentLoop(
            llm_wrapper=wrapper,
            tools=[],
            budget=_make_budget(),
            run_context=_make_ctx(),
        )
        result = await loop.run(system_prompt="sys", user_message="q")
        assert result.degraded
        assert "validate_failed" in result.degrade_reason


class TestLoopDetectorStop:
    """六类检测第二次命中 -> 停止并降级。"""

    @pytest.mark.asyncio
    async def test_exact_repeat_second_hit_stops(self):
        tool_response = AIMessage(
            content="",
            tool_calls=[_tool_call("search_knowledge", "c1", {"product_id": "p1"})],
        )
        final_answer = AIMessage(content="降级回答。", tool_calls=[])
        wrapper, llm = _make_wrapper(tool_response)
        # 4 次相同工具调用（第2次 replan 后 reset、第4次再次命中 stop）+ 1 次 finalization
        llm.ainvoke = AsyncMock(
            side_effect=[tool_response, tool_response, tool_response, tool_response, final_answer]
        )

        tool = _Tool("search_knowledge")
        loop = AgentLoop(
            llm_wrapper=wrapper,
            tools=[tool],
            budget=_make_budget(max_rounds=10),
            run_context=_make_ctx(allowed_product_ids=frozenset({"p1"})),
            max_repeated_actions=5,  # 避免旧式重复拦截提前触发
        )
        result = await loop.run(system_prompt="sys", user_message="q")
        assert result.degraded
        assert result.degrade_reason.startswith("loop_stopped")


class TestFinalizationNoReserve:
    """无 finalization 预留预算：不调用模型，返回结构化 budget_exhausted。"""

    @pytest.mark.asyncio
    async def test_no_finalization_budget(self):
        tool_response = AIMessage(
            content="",
            tool_calls=[_tool_call("search_knowledge", "c1", {})],
        )
        wrapper, llm = _make_wrapper(tool_response)
        llm.ainvoke = AsyncMock(side_effect=[tool_response])

        tool = _Tool("search_knowledge")
        loop = AgentLoop(
            llm_wrapper=wrapper,
            tools=[tool],
            budget=_make_budget(max_input_tokens=1, max_rounds=10),
            run_context=_make_ctx(),
            budget_plan=BudgetPlan(
                max_input_tokens=1,
                max_steps=10,
                max_runtime_seconds=60,
                finalization_reserve_tokens=0,
            ),
        )
        result = await loop.run(system_prompt="sys", user_message="q")
        assert result.status == LoopStatus.BUDGET_EXCEEDED
        assert result.degraded
        assert result.answer == ""
        # 仅 1 次 LLM 调用（决策轮），finalize 未调用模型
        assert llm.ainvoke.await_count == 1


class TestBudgetMetrics:
    """LLM 调用进入预算结算与指标汇总。"""

    @pytest.mark.asyncio
    async def test_metrics_recorded(self):
        response = AIMessage(content="回答。", tool_calls=[])
        wrapper, _ = _make_wrapper(response)
        loop = AgentLoop(
            llm_wrapper=wrapper,
            tools=[],
            budget=_make_budget(),
            run_context=_make_ctx(),
        )
        result = await loop.run(system_prompt="sys", user_message="q")
        metrics = loop.budget_manager.to_metrics()
        assert metrics["input_tokens"] > 0
        assert metrics["reservations"] >= 1
        assert result.usage.input_tokens == metrics["input_tokens"]


class TestEventStoreIntegration:
    """Loop 事件统一落库（fire-and-forget）。"""

    @pytest.mark.asyncio
    async def test_events_flushed_to_store(self):
        class FakeCollection:
            def __init__(self):
                self.inserted: list[dict] = []

            async def insert_one(self, doc):
                self.inserted.append(doc)

        class FakeDB(dict):
            pass

        coll = FakeCollection()
        db = FakeDB()
        db[COLLECTION] = coll
        store = AgentEventStore(db, collection=COLLECTION)

        response = AIMessage(content="回答。", tool_calls=[])
        wrapper, _ = _make_wrapper(response)
        loop = AgentLoop(
            llm_wrapper=wrapper,
            tools=[],
            budget=_make_budget(),
            run_context=_make_ctx(),
            event_store=store,
        )
        result = await loop.run(system_prompt="sys", user_message="q")
        assert result.status == LoopStatus.COMPLETED
        assert len(coll.inserted) > 0
        types = {doc["event_type"] for doc in coll.inserted}
        assert "loop_started" in types
        assert "loop_ended" in types
