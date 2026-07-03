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
import os
import sys

# ── Windows GBK → UTF-8 ──
if sys.platform == "win32":
    try:
        sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
        sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", buffering=1)
    except (OSError, AttributeError):
        pass

# 将 wewe_mcp 所在目录加入 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

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

    # 1. connect MCP Server
    client = MultiServerMCPClient(MCP_CONFIG)
    tools = await client.get_tools()

    for _t in tools:
        pass

    # 2. Create ReAct Agent
    agent = create_agent(llm, tools)

    # 3. 让 Agent 调用 MCP 工具

    queries = [
        "请用 check_accounts 工具检测一下 WeWe RSS 的账号状态，并告诉我结果。",
    ]

    for q in queries:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": q}]})
        messages = result.get("messages", [])
        ai_msg = messages[-1].content if messages else "no response"
        # 终端可能是 GBK，结果写入 UTF-8 文件
        output_file = "agent_output.txt"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(ai_msg)


    # MCP 连接会在进程退出时自动关闭


if __name__ == "__main__":
    asyncio.run(main())
