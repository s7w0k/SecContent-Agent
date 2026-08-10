"""LLMWrapper.invoke_agent_step 单元测试 -- 阶段一 Step 3。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.agent_contracts import RunContext
from agent.llm_wrapper import LLMWrapper
from agent.retry import RetryPolicy


def _make_run_context(**kwargs) -> RunContext:
    defaults = dict(trace_id="trace-1", run_id="run-1", user_id="user-1")
    defaults.update(kwargs)
    return RunContext(**defaults)


def _make_mock_llm(response: AIMessage) -> MagicMock:
    """创建 mock bound_llm，ainvoke 返回指定响应。"""
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=response)
    llm.model_name = "deepseek-chat"
    return llm


class TestInvokeAgentStep:
    """invoke_agent_step 核心测试。"""

    @pytest.mark.asyncio
    async def test_returns_ai_message(self):
        """正常调用返回 AIMessage。"""
        expected = AIMessage(content="回答内容", tool_calls=[])
        bound_llm = _make_mock_llm(expected)
        wrapper = LLMWrapper(llm=MagicMock(), db=None)

        response = await wrapper.invoke_agent_step(
            bound_llm=bound_llm,
            messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
            run_context=_make_run_context(),
            loop_round=0,
        )

        assert isinstance(response, AIMessage)
        assert response.content == "回答内容"

    @pytest.mark.asyncio
    async def test_tool_calls_extracted(self):
        """tool_calls 被正确提取到日志（通过 db 写入验证）。"""
        expected = AIMessage(
            content="",
            tool_calls=[{"name": "search_knowledge", "id": "call_001", "args": {"q": "test"}, "type": "tool_call"}],
        )
        bound_llm = _make_mock_llm(expected)

        # mock db
        db = MagicMock()
        db["llm_call_logs"] = MagicMock()
        db["llm_call_logs"].insert_one = AsyncMock()

        wrapper = LLMWrapper(llm=MagicMock(), db=db)
        await wrapper.invoke_agent_step(
            bound_llm=bound_llm,
            messages=[HumanMessage(content="hi")],
            run_context=_make_run_context(),
            loop_round=1,
        )

        # 验证日志写入
        db["llm_call_logs"].insert_one.assert_called_once()
        doc = db["llm_call_logs"].insert_one.call_args[0][0]
        assert doc["tool_names"] == ["search_knowledge"]
        assert doc["loop_round"] == 1
        assert doc["run_id"] == "run-1"
        assert doc["trace_id"] == "trace-1"
        assert doc["user_id"] == "user-1"
        assert "retry" in doc
        assert len(doc["retry"]) == 0  # 无重试

    @pytest.mark.asyncio
    async def test_log_excludes_prompt_content(self):
        """日志不包含原始 prompt 内容。"""
        expected = AIMessage(content="回答")
        bound_llm = _make_mock_llm(expected)

        db = MagicMock()
        db["llm_call_logs"] = MagicMock()
        db["llm_call_logs"].insert_one = AsyncMock()

        wrapper = LLMWrapper(llm=MagicMock(), db=db)
        await wrapper.invoke_agent_step(
            bound_llm=bound_llm,
            messages=[SystemMessage(content="SECRET_SYSTEM_PROMPT"), HumanMessage(content="SECRET_USER")],
            run_context=_make_run_context(),
        )

        doc = db["llm_call_logs"].insert_one.call_args[0][0]
        # system_prompt_hash 是 hash 不是原文
        assert "SECRET_SYSTEM_PROMPT" not in str(doc)
        assert "SECRET_USER" not in str(doc)
        assert doc["system_prompt_hash"].startswith("sha256:")

    @pytest.mark.asyncio
    async def test_token_usage_from_provider(self):
        """优先从 response.usage_metadata 读取 token。"""
        expected = AIMessage(
            content="回答",
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        )
        bound_llm = _make_mock_llm(expected)

        db = MagicMock()
        db["llm_call_logs"] = MagicMock()
        db["llm_call_logs"].insert_one = AsyncMock()

        wrapper = LLMWrapper(llm=MagicMock(), db=db)
        await wrapper.invoke_agent_step(
            bound_llm=bound_llm,
            messages=[HumanMessage(content="hi")],
            run_context=_make_run_context(),
        )

        doc = db["llm_call_logs"].insert_one.call_args[0][0]
        assert doc["input_tokens"] == 100
        assert doc["output_tokens"] == 50
        assert doc["total_tokens"] == 150

    @pytest.mark.asyncio
    async def test_token_estimated_when_no_usage(self):
        """无 usage_metadata 时按字符数估算。"""
        expected = AIMessage(content="abc")  # 3 chars -> ~1 token
        bound_llm = _make_mock_llm(expected)

        db = MagicMock()
        db["llm_call_logs"] = MagicMock()
        db["llm_call_logs"].insert_one = AsyncMock()

        wrapper = LLMWrapper(llm=MagicMock(), db=db)
        await wrapper.invoke_agent_step(
            bound_llm=bound_llm,
            messages=[HumanMessage(content="hello world"),],  # 11 chars -> ~2 tokens
            run_context=_make_run_context(),
        )

        doc = db["llm_call_logs"].insert_one.call_args[0][0]
        assert doc["input_tokens"] > 0
        assert doc["output_tokens"] > 0

    @pytest.mark.asyncio
    async def test_exception_propagates(self):
        """LLM 调用失败时异常传播，日志仍写入。"""
        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(side_effect=TimeoutError("timeout"))

        db = MagicMock()
        db["llm_call_logs"] = MagicMock()
        db["llm_call_logs"].insert_one = AsyncMock()

        wrapper = LLMWrapper(llm=MagicMock(), db=db)

        with pytest.raises(TimeoutError):
            await wrapper.invoke_agent_step(
                bound_llm=bound_llm,
                messages=[HumanMessage(content="hi")],
                run_context=_make_run_context(),
                retry_policy=RetryPolicy(max_attempts=2, base_delay=0.01, max_delay=0.01),
            )

        # 日志仍写入
        db["llm_call_logs"].insert_one.assert_called_once()
        doc = db["llm_call_logs"].insert_one.call_args[0][0]
        assert doc["degraded"] is True
        assert "failed" in doc["degrade_reason"].lower()

    @pytest.mark.asyncio
    async def test_log_failure_does_not_block(self):
        """日志写入失败不影响业务结果。"""
        expected = AIMessage(content="ok")
        bound_llm = _make_mock_llm(expected)

        db = MagicMock()
        db["llm_call_logs"] = MagicMock()
        db["llm_call_logs"].insert_one = AsyncMock(side_effect=Exception("db down"))

        wrapper = LLMWrapper(llm=MagicMock(), db=db)

        # 不抛异常
        response = await wrapper.invoke_agent_step(
            bound_llm=bound_llm,
            messages=[HumanMessage(content="hi")],
            run_context=_make_run_context(),
        )
        assert response.content == "ok"

    @pytest.mark.asyncio
    async def test_no_db_no_logging(self):
        """db=None 时不写日志，正常返回。"""
        expected = AIMessage(content="ok")
        bound_llm = _make_mock_llm(expected)
        wrapper = LLMWrapper(llm=MagicMock(), db=None)

        response = await wrapper.invoke_agent_step(
            bound_llm=bound_llm,
            messages=[HumanMessage(content="hi")],
            run_context=_make_run_context(),
        )
        assert response.content == "ok"

    @pytest.mark.asyncio
    async def test_invoke_structured_unchanged(self):
        """现有 invoke_structured 行为不受影响。"""
        from agent.schemas import SingleProductScoreSchema

        mock_llm = MagicMock()
        mock_llm.model_name = "deepseek-chat"
        mock_llm.with_structured_output = MagicMock()
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(return_value={"relevance": 85, "event_impact": 70, "reason": "test"})
        mock_llm.with_structured_output.return_value = structured_llm

        wrapper = LLMWrapper(llm=mock_llm, db=None)
        result = await wrapper.invoke_structured(
            system_prompt="sys",
            user_prompt="user",
            output_schema=SingleProductScoreSchema,
            agent_type="scorer_v2",
        )

        assert result.relevance == 85
        assert result.event_impact == 70
