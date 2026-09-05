#!/usr/bin/env python
"""Provider 工具调用能力探针（阶段一 Step 1）。

验证 DeepSeek (deepseek-chat) 兼容端点的工具调用能力，为 AgentLoop 实现提供决策依据。

测试项：
  1. bind_tools().ainvoke() 与 AIMessage.tool_calls
  2. tool name/id/args 格式
  3. 同轮多工具
  4. astream() 的 tool call chunk
  5. tool_choice=auto/none
  6. usage metadata
  7. 异常类型（超时、无效 schema）

安全约束：
  - 只在非生产环境执行
  - 不记录 API Key 和用户内容
  - 结果写入 stdout，人工填写到 provider-capability-matrix.md

运行方式：
  cd pr-agent-demo-v2
  python scripts/probe_chat_tool_calling.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

# ── 工作目录断言 ──────────────────────────────────────────────
_CWD = os.getcwd()
if "pr-agent-demo-v2" not in _CWD:
    print(f"ERROR: 必须在 pr-agent-demo-v2 目录下运行，当前: {_CWD}", file=sys.stderr)
    sys.exit(1)

# ── 依赖检查 ──────────────────────────────────────────────────
try:
    from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
except ImportError as e:
    print(f"ERROR: 缺少依赖: {e}", file=sys.stderr)
    print("请运行: pip install langchain-openai langchain-core", file=sys.stderr)
    sys.exit(1)

# ── 配置（从环境变量读取，不硬编码）──────────────────────────
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

if not API_KEY or API_KEY.startswith("sk-your-"):
    print("ERROR: 请设置 DEEPSEEK_API_KEY 环境变量", file=sys.stderr)
    sys.exit(1)


# ── 测试用工具定义 ────────────────────────────────────────────


@tool
def search_knowledge(product_name: str) -> str:
    """搜索产品知识库，返回产品定位和核心功能摘要。当用户询问产品能力时使用。
    Args:
        product_name: 产品名称
    """
    return (
        f"[知识库] {product_name}: 智能体身份安全产品，核心能力包括身份认证、权限管控、意图识别。"
    )


@tool
def get_article(url_hash: str) -> str:
    """查询文章详情（标题、摘要、正文前段）。当需要了解文章内容时使用。
    Args:
        url_hash: 文章 URL hash
    """
    return f"[文章] {url_hash}: 智能体安全重大事件报道，涉及提示注入攻击。"


@tool
def retrieve_memory(category: str = "") -> str:
    """检索当前用户的记忆偏好。当需要个性化回答时使用。
    Args:
        category: 可选，按分类过滤
    """
    return "[记忆] 用户偏好: 简洁风格，关注技术细节。"


ALL_TOOLS = [search_knowledge, get_article, retrieve_memory]


# ── 辅助 ──────────────────────────────────────────────────────


def _make_llm(**kwargs: Any) -> ChatOpenAI:
    """创建 ChatOpenAI 实例。"""
    defaults = {
        "model": MODEL,
        "api_key": API_KEY,
        "base_url": BASE_URL,
        "temperature": 0.1,
        "max_tokens": 500,
    }
    defaults.update(kwargs)
    return ChatOpenAI(**defaults)


def _print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _print_result(label: str, value: Any, ok: bool | None = None) -> None:
    status = ""
    if ok is True:
        status = " [PASS]"
    elif ok is False:
        status = " [FAIL]"
    raw_repr = repr(value)
    display = (
        raw_repr
        if isinstance(value, (str, dict, list, type(None))) and len(raw_repr) > 100
        else value
    )
    print(f"  {label}: {display}{status}")


# ── 测试用例 ──────────────────────────────────────────────────


async def test_1_basic_tool_call() -> dict:
    """测试 1: bind_tools().ainvoke() 与 AIMessage.tool_calls"""
    _print_section("测试 1: bind_tools + ainvoke + tool_calls")
    result: dict[str, Any] = {"pass": False}

    try:
        llm = _make_llm()
        bound = llm.bind_tools(ALL_TOOLS)
        messages = [
            SystemMessage(content="你是一个助手，可以使用工具回答问题。"),
            HumanMessage(content="智能体身份安全产品的核心能力是什么？请查询知识库。"),
        ]
        response = await bound.ainvoke(messages)

        is_ai_message = isinstance(response, AIMessage)
        _print_result("返回类型为 AIMessage", type(response).__name__, is_ai_message)

        tool_calls = getattr(response, "tool_calls", None)
        has_tool_calls = isinstance(tool_calls, list) and len(tool_calls) > 0
        _print_result(
            "tool_calls 存在且非空", f"len={len(tool_calls) if tool_calls else 0}", has_tool_calls
        )

        if has_tool_calls:
            tc = tool_calls[0]
            has_name = "name" in tc and tc["name"]
            has_id = "id" in tc and tc["id"]
            has_args = "args" in tc and isinstance(tc["args"], dict)
            _print_result("tool_call.name", tc.get("name", ""), has_name)
            _print_result("tool_call.id", tc.get("id", "")[:20] + "...", has_id)
            _print_result("tool_call.args", tc.get("args", {}), has_args)
            _print_result("tool_call.type", tc.get("type", ""), True)

        content_preview = (
            (response.content or "")[:80]
            if isinstance(response.content, str)
            else str(response.content)[:80]
        )
        _print_result("content 预览", content_preview)

        result["pass"] = is_ai_message and has_tool_calls and has_name and has_id and has_args
        result["tool_calls"] = tool_calls
    except Exception as e:
        _print_result("异常", f"{type(e).__name__}: {e}", False)
        result["error"] = str(e)

    return result


async def test_2_multiple_tool_calls() -> dict:
    """测试 2: 同轮多工具"""
    _print_section("测试 2: 同轮多工具调用")
    result: dict[str, Any] = {"pass": False}

    try:
        llm = _make_llm()
        bound = llm.bind_tools(ALL_TOOLS)
        messages = [
            SystemMessage(content="你是一个助手，需要同时查询多方面信息。"),
            HumanMessage(
                content="请同时帮我查询智能体身份安全产品的知识库，以及文章 abc123 的内容。"
            ),
        ]
        response = await bound.ainvoke(messages)
        tool_calls = getattr(response, "tool_calls", [])

        count = len(tool_calls)
        _print_result("tool_calls 数量", count, count >= 2)

        if count >= 2:
            ids = [tc.get("id", "") for tc in tool_calls]
            names = [tc.get("name", "") for tc in tool_calls]
            ids_unique = len(set(ids)) == len(ids)
            _print_result("tool names", names)
            _print_result("tool ids 唯一", ids_unique, ids_unique)

        result["pass"] = count >= 2
        result["tool_calls"] = tool_calls
    except Exception as e:
        _print_result("异常", f"{type(e).__name__}: {e}", False)
        result["error"] = str(e)

    return result


async def test_3_streaming_tool_calls() -> dict:
    """测试 3: astream() 的 tool call chunk"""
    _print_section("测试 3: 流式 tool call chunk")
    result: dict[str, Any] = {"pass": False, "stable": False}

    try:
        llm = _make_llm()
        bound = llm.bind_tools(ALL_TOOLS)
        messages = [
            SystemMessage(content="你是一个助手，可以使用工具回答问题。"),
            HumanMessage(content="请查询智能体身份安全产品的知识库。"),
        ]

        chunks: list[AIMessageChunk] = []
        tool_call_chunks: list[dict] = []

        async for chunk in bound.astream(messages):
            chunks.append(chunk)
            tc_chunks = getattr(chunk, "tool_call_chunks", None)
            if tc_chunks:
                tool_call_chunks.extend(tc_chunks)

        _print_result("收到 chunk 数量", len(chunks))
        _print_result("tool_call_chunks 数量", len(tool_call_chunks), len(tool_call_chunks) > 0)

        if tool_call_chunks:
            _print_result(
                "首个 chunk 预览", {k: v for k, v in tool_call_chunks[0].items() if k != "args"}
            )
            # 检查拼接后是否能还原完整 tool call
            assembled_names = set()
            for tc_chunk in tool_call_chunks:
                name = tc_chunk.get("name")
                if name:
                    assembled_names.add(name)
            _print_result("拼接后 tool names", assembled_names, len(assembled_names) > 0)

            # 尝试用最后 chunk 的 tool_calls 做完整性判断
            last_chunk = chunks[-1] if chunks else None
            final_tool_calls = getattr(last_chunk, "tool_calls", []) if last_chunk else []
            has_complete = len(final_tool_calls) > 0
            _print_result("最后 chunk 含完整 tool_calls", has_complete, has_complete)
            result["stable"] = has_complete

        result["pass"] = len(tool_call_chunks) > 0
    except Exception as e:
        _print_result("异常", f"{type(e).__name__}: {e}", False)
        result["error"] = str(e)

    return result


async def test_4_tool_choice() -> dict:
    """测试 4: tool_choice=auto/none"""
    _print_section("测试 4: tool_choice 参数")
    result: dict[str, Any] = {"auto": False, "none": False}

    # auto: 模型自主决定
    try:
        llm = _make_llm()
        bound = llm.bind_tools(ALL_TOOLS, tool_choice="auto")
        messages = [
            SystemMessage(content="你是一个助手。"),
            HumanMessage(content="你好，今天天气怎么样？"),
        ]
        response = await bound.ainvoke(messages)
        tool_calls = getattr(response, "tool_calls", [])
        no_tool = len(tool_calls) == 0
        _print_result("tool_choice=auto (常识问题)", f"tool_calls={len(tool_calls)}", True)
        result["auto"] = True
    except Exception as e:
        _print_result("tool_choice=auto 异常", f"{type(e).__name__}: {e}", False)
        result["auto_error"] = str(e)

    # none: 禁止工具
    try:
        llm = _make_llm()
        bound = llm.bind_tools(ALL_TOOLS, tool_choice="none")
        messages = [
            SystemMessage(content="你是一个助手。"),
            HumanMessage(content="请查询智能体身份安全产品的知识库。"),
        ]
        response = await bound.ainvoke(messages)
        tool_calls = getattr(response, "tool_calls", [])
        no_tool = len(tool_calls) == 0
        _print_result("tool_choice=none (强制不用工具)", f"tool_calls={len(tool_calls)}", no_tool)
        result["none"] = no_tool
    except Exception as e:
        _print_result("tool_choice=none 异常", f"{type(e).__name__}: {e}", False)
        result["none_error"] = str(e)

    return result


async def test_5_usage_metadata() -> dict:
    """测试 5: usage metadata"""
    _print_section("测试 5: Usage Metadata")
    result: dict[str, Any] = {"has_usage": False}

    try:
        llm = _make_llm()
        bound = llm.bind_tools(ALL_TOOLS)
        messages = [
            SystemMessage(content="你是一个助手。"),
            HumanMessage(content="请查询智能体身份安全产品的知识库。"),
        ]
        response = await bound.ainvoke(messages)

        usage_meta = getattr(response, "usage_metadata", None)
        _print_result("usage_metadata", usage_meta, usage_meta is not None)

        response_meta = getattr(response, "response_metadata", None)
        _print_result(
            "response_metadata 存在", response_meta is not None, response_meta is not None
        )
        if response_meta:
            token_usage = response_meta.get("token_usage") or response_meta.get("usage")
            _print_result("token_usage", token_usage, token_usage is not None)

        result["has_usage"] = usage_meta is not None
        result["usage_metadata"] = usage_meta
        result["response_metadata"] = response_meta
    except Exception as e:
        _print_result("异常", f"{type(e).__name__}: {e}", False)
        result["error"] = str(e)

    return result


async def test_6_timeout_exception() -> dict:
    """测试 6: 超时异常类型"""
    _print_section("测试 6: 超时异常")
    result: dict[str, Any] = {"exception_type": None}

    try:
        llm = _make_llm(timeout=0.001, max_retries=0)  # 极短超时
        messages = [
            SystemMessage(content="你是一个助手。"),
            HumanMessage(content="请回答一个复杂问题：..."),
        ]
        await llm.ainvoke(messages)
        _print_result("未触发超时（可能网络过快）", "N/A")
    except Exception as e:
        exc_type = type(e).__name__
        _print_result("异常类型", exc_type, True)
        _print_result("异常基类", [base.__name__ for base in type(e).__mro__[:5]])
        _print_result("异常消息", str(e)[:100])
        result["exception_type"] = exc_type
        result["exception"] = str(e)

    return result


async def test_7_invalid_schema() -> dict:
    """测试 7: 无效 tool schema"""
    _print_section("测试 7: 无效 Tool Schema")
    result: dict[str, Any] = {"exception_type": None}

    try:
        # 定义一个 schema 不合法的 tool（参数无类型注解）
        @tool
        def bad_tool(untyped_param) -> str:
            """描述"""
            return "bad"

        llm = _make_llm()
        bound = llm.bind_tools([bad_tool])
        messages = [
            SystemMessage(content="你是一个助手。"),
            HumanMessage(content="请使用 bad_tool。"),
        ]
        await bound.ainvoke(messages)
        _print_result("未异常（可能 provider 容错）", "N/A")
        result["exception_type"] = "none"
    except Exception as e:
        exc_type = type(e).__name__
        _print_result("异常类型", exc_type, True)
        _print_result("异常消息", str(e)[:120])
        result["exception_type"] = exc_type
        result["exception"] = str(e)

    return result


# ── 主流程 ────────────────────────────────────────────────────


async def main() -> None:
    print("\nProvider 工具调用能力探针")
    print(f"  Model: {MODEL}")
    print(f"  Base URL: {BASE_URL}")
    print(f"  API Key: {'***' + API_KEY[-4:] if API_KEY else '(空)'}")
    print(f"  Working dir: {_CWD}")

    results: dict[str, dict] = {}

    results["test_1_basic"] = await test_1_basic_tool_call()
    results["test_2_multi"] = await test_2_multiple_tool_calls()
    results["test_3_stream"] = await test_3_streaming_tool_calls()
    results["test_4_choice"] = await test_4_tool_choice()
    results["test_5_usage"] = await test_5_usage_metadata()
    results["test_6_timeout"] = await test_6_timeout_exception()
    results["test_7_schema"] = await test_7_invalid_schema()

    # ── 汇总 ──────────────────────────────────────────────────
    _print_section("汇总")

    t1 = results["test_1_basic"]
    _print_result("1. bind_tools + ainvoke", t1.get("pass", False), t1.get("pass"))

    t2 = results["test_2_multi"]
    _print_result("2. 同轮多工具", t2.get("pass", False), t2.get("pass"))

    t3 = results["test_3_stream"]
    _print_result("3. 流式 tool call", t3.get("pass", False), t3.get("pass"))
    _print_result("3a. 流式稳定性（最后 chunk 完整）", t3.get("stable", False), t3.get("stable"))

    t4 = results["test_4_choice"]
    _print_result("4a. tool_choice=auto", t4.get("auto", False), t4.get("auto"))
    _print_result("4b. tool_choice=none", t4.get("none", False), t4.get("none"))

    t5 = results["test_5_usage"]
    _print_result("5. usage_metadata", t5.get("has_usage", False), t5.get("has_usage"))

    t6 = results["test_6_timeout"]
    _print_result("6. 超时异常类型", t6.get("exception_type", "N/A"))

    t7 = results["test_7_schema"]
    _print_result("7. 无效 schema 异常类型", t7.get("exception_type", "N/A"))

    # ── 决策建议 ──────────────────────────────────────────────
    _print_section("1A 策略建议")

    stream_stable = t3.get("stable", False)
    if stream_stable:
        print("  → 流式 tool call 稳定：可使用流式决策轮")
    else:
        print("  → 流式 tool call 不稳定：1A 使用**非流式决策轮** + 流式 text_delta 输出")
        print("    即：每轮用 ainvoke（非流式）做工具决策，最终回答用 astream 输出 text_delta")

    usage_available = t5.get("has_usage", False)
    if usage_available:
        print("  → usage_metadata 可读：直接读取 provider token 计数")
    else:
        print("  → usage_metadata 不可读：token 计数需估算（按字符数 / 4 粗估）")

    print("\n  请将以上结果填写到 docs/agent-loop/provider-capability-matrix.md")


if __name__ == "__main__":
    asyncio.run(main())
