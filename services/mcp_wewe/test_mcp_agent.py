"""
LangChain + DeepSeek Agent —— verify wewe_mcp MCP 服务是否可用。

用法：
    python test_mcp_agent.py

流程：
    1. 通过 stdio connect wewe_mcp_server.py
    2. 获取 MCP 工具列表，包装为 LangChain Tool
    3. 使用 DeepSeek 作为 LLM，Create ReAct Agent
    4. 让Agent调用 check_accounts 检测账号状态
"""

import asyncio
import sys
import os

# ── Windows GBK → UTF-8 ──
if sys.platform == "win32":
    try:
        sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
        sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", buffering=1)
    except (OSError, AttributeError):
        pass

# 将 wewe_mcp 所在目录加入 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient


# ===============================================================
# DeepSeek LLM
# ===============================================================
llm = ChatOpenAI(
    model="deepseek-chat",
    api_key="sk-REDACTED",
    base_url="https://api.deepseek.com/v1",
    temperature=0.3,
)


# ===============================================================
# MCP 客户端配置（stdio 方式connect wewe_mcp_server.py）
# ===============================================================
MCP_CONFIG = {
    "wewe-rss": {
        "transport": "stdio",
        "command": sys.executable,  # 用同一个 Python 解释器
        "args": [os.path.join(os.path.dirname(__file__), "wewe_mcp_server.py")],
    }
}


async def main():
    print("=" * 60)
    print("LangChain + DeepSeek Agent —— MCP verify")
    print("=" * 60)

    # 1. connect MCP Server
    print("\n[1] connect MCP Server (wewe_mcp_server.py via stdio)...")
    client = MultiServerMCPClient(MCP_CONFIG)
    tools = await client.get_tools()

    print(f"    Got {len(tools)} MCP tools:")
    for t in tools:
        print(f"      - {t.name}: {t.description[:60]}...")

    # 2. Create ReAct Agent
    print("\n[2] Create ReAct Agent (LLM: deepseek-chat)...")
    agent = create_agent(llm, tools)

    # 3. 让 Agent 调用 MCP 工具
    print("\n[3] Ask agent to check WeWe RSS account status\n")

    queries = [
        "请用 check_accounts 工具检测一下 WeWe RSS 的账号状态，并告诉我结果。",
    ]

    for q in queries:
        print(f"    [User]: {q}")
        result = await agent.ainvoke({"messages": [{"role": "user", "content": q}]})
        messages = result.get("messages", [])
        ai_msg = messages[-1].content if messages else "no response"
        # 终端可能是 GBK，结果写入 UTF-8 文件
        output_file = "agent_output.txt"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(ai_msg)
        print(f"    [Agent response written to: {output_file}]")
        print(f"    [Preview]: {ai_msg[:150]}")
        print()

    print("=" * 60)
    print("MCP verification complete! Agent successfully used check_accounts tool.")

    # MCP 连接会在进程退出时自动关闭


if __name__ == "__main__":
    asyncio.run(main())
