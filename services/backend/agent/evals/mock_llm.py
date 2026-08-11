"""确定性 Mock LLM 模拟器 -- 阶段2 真实 Eval Runner 的离线后端（WBS 2.1）。

设计目标（对齐阶段2 §4 / 退出门禁）：
  - evaluator 不再生成「必然通过」的 mock 结果作为发布证据：
    即使使用模拟器，也必须真实执行 AgentLoop 状态机（预留/结算/工具执行/
    事件落库/终态），评测结果反映的是状态机的正确性，而非注入的假通过；
  - legacy 与 candidate 使用同一模拟器契约（同输入、同 fixture）；
  - 模拟器行为完全确定（无随机），按 EvalCase.input_fixture.tool_script 的
    剧本逐轮返回工具调用，剧本用尽后返回最终答案，保证可重复；
  - 支持故障注入（llm_error: rate_limit/timeout/empty），用于 budget_limits
    与 reliability 类用例。

真实后端（real）在 runner 中通过 LangChain ChatOpenAI 构造，本模块不涉及。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

MAX_TOOL_CALLS_PER_ROUND = 4


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _count_chars(messages: list[Any]) -> int:
    total = 0
    for msg in messages:
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            total += len(content)
    return total


def _tool_call(name: str, call_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "id": call_id, "args": args or {}, "type": "tool_call"}


class MockToolLLM:
    """可绑定工具的确定性模拟器（用于 candidate AgentLoop 决策轮）。

    Args:
        model_name: 模拟的模型名（成本计价用）
        tool_script: 每轮工具调用剧本 [[工具名...], ...]，轮次用尽后输出答案
        answer_builder: (question, required_facts) -> str，生成最终答案
        final_answer: 固定最终答案（优先于 answer_builder）
        fault: 故障注入 {llm_error: "rate_limit"|"timeout"|"empty"}
    """

    def __init__(
        self,
        model_name: str = "deepseek-chat",
        tool_script: list[list[str]] | None = None,
        answer_builder: Callable[[str, list[str]], str] | None = None,
        final_answer: str = "",
        fault: dict[str, Any] | None = None,
    ):
        self.model_name = model_name
        self.tool_script = tool_script or []
        self.answer_builder = answer_builder
        self.final_answer = final_answer
        self.fault = fault or {}
        self.bound_tools: list[str] = []
        self._finalization_mode = False  # bind_tools([], tool_choice="none") 表示 finalization 轮
        self._calls = 0
        self._tool_seq = 0

    # ── langchain 契约 ─────────────────────────────────────

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> MockToolLLM:
        """记录可用工具名，返回自身（模拟 bind 语义，兼容 tool_choice 等参数）。

        AgentLoop._finalize 以 bind_tools([], tool_choice="none") 发起 finalization 轮；
        故障注入仅作用于决策轮（finalization 轮恢复，用于 finalization 降级答案类用例）。
        """
        self.bound_tools = [getattr(t, "name", str(t)) for t in tools]
        self._finalization_mode = kwargs.get("tool_choice") == "none"
        return self

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        """决策轮调用：按剧本返回工具调用，剧本用尽返回最终答案。"""
        from langchain_core.messages import ToolMessage

        call_no = self._calls
        self._calls += 1
        fault_error = self.fault.get("llm_error")

        # 故障注入：仅决策轮持续触发（含重试），finalization 轮恢复。
        # 保证预算类用例可复现（timeout 经重试耗尽后仍失败 → budget_exceeded）。
        if not self._finalization_mode and fault_error in ("rate_limit", "timeout"):
            if fault_error == "timeout":
                raise TimeoutError("mock llm timeout")
            raise RuntimeError("mock llm rate_limit: 429")

        tool_groups = [g for g in self.tool_script if g]
        if call_no < len(tool_groups):
            group = tool_groups[call_no]
            tool_calls = [
                _tool_call(name, f"mock-{call_no}-{i}", self._args_for(name))
                for i, name in enumerate(group)
            ]
            return self._message(content="", tool_calls=tool_calls, messages=messages)

        # 剧本用尽：输出最终答案（受工具观察影响；如已有工具结果则引用它们）
        observed = [m for m in messages if isinstance(m, ToolMessage)]
        if fault_error == "empty" and call_no == 0:
            return self._message(content="", tool_calls=[], messages=messages)
        answer = self.final_answer
        if not answer and self.answer_builder is not None:
            answer = self.answer_builder(str(messages[-1].content), observed)
        return self._message(content=answer, tool_calls=[], messages=messages)

    # ── 内部 ───────────────────────────────────────────────

    def _args_for(self, name: str) -> dict[str, Any]:
        """为剧本中的工具生成确定性参数（与 fixture 工具签名对齐）。"""
        if name == "search_knowledge":
            return {"product_id": "p_eval"}
        if name == "get_article":
            return {"url_hash": "eval-article-1"}
        if name == "retrieve_memory":
            return {"category": ""}
        return {"query": name}

    def _message(
        self,
        *,
        content: str,
        tool_calls: list[dict[str, Any]],
        messages: list[BaseMessage],
    ) -> AIMessage:
        input_tokens = max(1, _count_chars(messages) // 4)
        output_tokens = max(1, len(content) // 4) if content else 0
        return AIMessage(
            content=content,
            tool_calls=tool_calls,
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "input_token_details": {"cache_read": 0},
            },
        )


class MockLegacyLLM:
    """legacy 单轮路径模拟器（无工具绑定，直接给出最终答案）。"""

    def __init__(
        self,
        model_name: str = "deepseek-chat",
        answer_builder: Callable[[str, list[str]], str] | None = None,
        final_answer: str = "",
        fault: dict[str, Any] | None = None,
    ):
        self.model_name = model_name
        self.answer_builder = answer_builder
        self.final_answer = final_answer
        self.fault = fault or {}

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        if self.fault.get("llm_error") in ("rate_limit", "timeout"):
            raise RuntimeError("mock legacy llm failed")
        last = messages[-1] if messages else None
        question = str(getattr(last, "content", ""))
        answer = self.final_answer
        if not answer and self.answer_builder is not None:
            answer = self.answer_builder(question, [])
        input_tokens = max(1, _count_chars(messages) // 4)
        output_tokens = max(1, len(answer) // 4) if answer else 0
        return AIMessage(
            content=answer,
            tool_calls=[],
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "input_token_details": {"cache_read": 0},
            },
        )


def default_answer_builder(question: str, required_facts: list[str]) -> str:
    """默认最终答案构造：基于必需事实生成确定文本（可重复）。"""
    if not required_facts:
        return "已根据可用信息完成回答。"
    return "基于资料：" + "；".join(required_facts) + "。"
