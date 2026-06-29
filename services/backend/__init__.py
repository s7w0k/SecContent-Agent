"""backend: 智能体安全 PR 情报 Agent 系统核心后端

FastAPI + LangChain + MongoDB，提供：
- REST API（仪表盘数据、流水线触发、PR 报告 CRUD）
- Agent Pipeline（爬取 → 分类 → 打分 → 报道生成）
- MCP Client（连接 mcp-wewe 和 mcp-crawl）
- 静态文件服务（React 构建产物）
"""
