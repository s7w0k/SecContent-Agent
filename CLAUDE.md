# CLAUDE.md — PR Agent Demo 项目上下文

## 项目简介

智能体安全PR情报Agent系统 Demo — 从**内容爬取 → 入库 → Agent分析打分 → PR报道生成 → 可视化展示**的端到端自动化系统。

- **仓库**: https://gitee.com/s7w0k/pr-agent-demo
- **部署方式**: Docker Compose 一键启动
- **技术栈**: Python 3.12 / FastAPI / LangChain / MongoDB / React + Ant Design / DeepSeek

## 架构概览

```
docker compose up
├── mongodb:27017     — 数据持久化（articles / reports / knowledge_base）
├── mcp-wewe:8100     — 微信公众号 RSS MCP HTTP 桥接
├── mcp-crawl:8101    — 海外安全新闻爬虫 MCP HTTP 桥接
└── backend:8000      — 核心服务：Agent 流水线 + REST API + 静态前端
```

## 目录结构

```
pr-agent-demo/
├── services/
│   ├── mcp_wewe/          # 微信公众号RSS MCP服务（已有，基于wewe_mcp）
│   ├── mcp_crawl/         # 海外安全新闻MCP服务（新建，封装site_crawl）
│   └── backend/           # 核心后端（FastAPI + LangChain Agent）
│       ├── api/           # REST API 路由
│       ├── agent/         # Agent 流水线（LangGraph）
│       ├── models/        # MongoDB 数据模型（pydantic）
│       └── db/            # 数据库连接管理
├── frontend/              # React + Ant Design 仪表盘
├── mongodb/               # 数据库初始化脚本
├── docs/                  # 文档
│   └── 阶段一/            # 阶段一精细化计划
├── tests/                 # 测试
├── scripts/               # 运维脚本
└── docker-compose.yml     # 容器编排
```

## 编码约定

### Python
- **格式化/Lint**: `ruff`（配置在 `pyproject.toml`）
- **类型注解**: 所有函数签名使用类型注解
- **异步**: 服务端统一使用 `async/await`（FastAPI + Motor）
- **配置**: 通过 `pydantic-settings` 从环境变量加载，在 `config.py` 中集中管理
- **日志**: 使用 `logging` 模块，禁止 `print`
- **异常**: 不裸写 `except:`，明确异常类型

### TypeScript（前端）
- **格式化/Lint**: `biome`（配置在 `frontend/biome.json`）
- **类型**: 启用 strict mode
- **组件**: 使用 Ant Design 组件库

### 通用
- **Commit**: Conventional Commits 格式（`feat:`, `fix:`, `chore:` 等）
- **分支**: trunk-based（`main` + `feat/*`）
- **密钥**: 禁止硬编码，通过 `.env` 注入
- **Dockerfile**: 优先 multi-stage build 减小镜像体积

## 常用命令

```bash
make dev          # 一键启动开发环境
make test         # 运行全部测试
make lint         # 代码检查（ruff + biome）
make format       # 自动格式化
make build        # 构建 Docker 镜像
make up           # 启动生产环境
make down         # 停止所有服务
```

## 关键依赖

| 服务 | 依赖 |
|------|------|
| mcp-wewe | DeepSeek API（AI 摘要）、WeWe RSS |
| mcp-crawl | Tavily API（搜索）、DeepSeek API（分类） |
| backend | DeepSeek API（打分/报道）、MongoDB |

## 文档索引

- [技术方案与实施计划](docs/技术方案与实施计划.md)
- [智能体身份安全产品计划](docs/智能体身份安全产品计划和目标.md)
- [阶段一：精细化实施计划](docs/阶段一/基础架构搭建-精细化实施计划.md)
- [API 契约规范](docs/阶段一/API契约规范.md)
