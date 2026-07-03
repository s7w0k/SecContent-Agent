"""
LangChain Agent —— 通过 HTTP 桥接测试远程 MCP 服务。

远程桥接地址: http://49.232.145.182:8080
本地运行此脚本即可验证远程 MCP 是否正常。
"""
import asyncio
import contextlib
import sys

if sys.platform == "win32":
    with contextlib.suppress(OSError, AttributeError):
        sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)

import httpx
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

# ═══════════════════════════════════════════
BRIDGE_URL = "http://49.232.145.182:6060"
# ═══════════════════════════════════════════


# ── 先检查桥接是否可达 ──

async def check_bridge() -> bool:
    """检测远程桥接服务是否在线。"""
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"{BRIDGE_URL}/tools")
            return resp.status_code == 200
        except Exception:
            return False


# ── 从 HTTP 桥接获取工具列表，动态创建 LangChain Tools ──

async def fetch_bridge_tools() -> list:
    """从桥接服务获取可用工具，包装为 LangChain Tools。"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BRIDGE_URL}/tools")
        resp.raise_for_status()
        data = resp.json()
        tools_meta = data.get("tools", {})

    langchain_tools = []
    for tool_name, meta in tools_meta.items():
        desc = meta.get("description", tool_name)
        meta.get("inputSchema", {})

        # 动态创建 LangChain StructuredTool
        from langchain_core.tools import StructuredTool

        def make_call_fn(name: str):
            async def _call(**kwargs):
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{BRIDGE_URL}/call/{name}",
                        json=kwargs if kwargs else {},
                    )
                    return resp.json()
            return _call

        tool_obj = StructuredTool.from_function(
            name=tool_name,
            description=desc,
            coroutine=make_call_fn(tool_name),
        )
        langchain_tools.append(tool_obj)

    return langchain_tools


# ── 主流程 ──

async def main():

    # 0. 检测连接
    if not await check_bridge():
        return

    # 1. 获取远程工具
    tools = await fetch_bridge_tools()
    for _t in tools:
        pass

    # 2. 创建 Agent (DeepSeek)
    llm = ChatOpenAI(
        model="deepseek-chat",
        api_key="sk-REDACTED",
        base_url="https://api.deepseek.com/v1",
        temperature=0.3,
    )
    agent = create_agent(llm, tools)

    # 3. 交互式提问

    messages = []
    while True:
        try:
            q = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not q:
            continue
        if q.lower() in ("quit", "exit", "q"):
            break

        messages.append({"role": "user", "content": q})
        try:
            result = await asyncio.wait_for(
                agent.ainvoke({"messages": list(messages)}),
                timeout=120,
            )
            msgs = result.get("messages", [])
            answer = msgs[-1].content if msgs else "(no response)"
            messages.append({"role": "assistant", "content": answer})
        except TimeoutError:
            pass



if __name__ == "__main__":
    asyncio.run(main())
