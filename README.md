# PR Agent Demo

智能体安全 PR 情报 Agent 系统 Demo — 从**内容爬取 → 入库 → Agent 分析打分 → PR 报道生成 → 可视化展示**的端到端自动化系统。

- **仓库**: https://gitee.com/s7w0k/pr-agent-demo
- **部署方式**: Docker Compose 一键启动
- **技术栈**: Python 3.12 / FastAPI / LangChain / MongoDB / React + Ant Design / DeepSeek

---

## 架构概览

```
docker compose up
├── mongodb:27017     — 数据持久化（articles / reports / knowledge_base）
├── mcp-wewe:8100     — 微信公众号 RSS MCP HTTP 桥接
├── mcp-crawl:8101    — 海外安全新闻爬虫 MCP HTTP 桥接
└── backend:8000      — API 网关 + Agent 流水线 + 静态前端
```

```
                       ┌──────────────┐
                       │   浏览器      │
                       └──────┬───────┘
                              │ :8000
                       ┌──────▼───────┐
                       │   Backend    │  FastAPI + Agent Pipeline
                       └──┬──────┬────┘
                          │      │
              ┌───────────┘      └───────────┐
              ▼                              ▼
     ┌────────────────┐            ┌────────────────┐
     │   mcp-wewe     │            │   mcp-crawl    │
     │   :8100 (内部)  │            │   :8101 (内部)  │
     └────────┬───────┘            └────────┬───────┘
              │                              │
              ▼                              ▼
     ┌────────────────┐            ┌────────────────┐
     │  WeWe RSS      │            │  海外安全新闻    │
     │  微信公众号      │            │  THN/BC/SW/HNS  │
     └────────────────┘            └────────────────┘
              │
              ▼
     ┌────────────────┐
     │   MongoDB       │
     │   :27017 (内部)  │
     └────────────────┘
```

---

## 快速开始

### 前置要求

- Docker Desktop ([下载](https://docs.docker.com/desktop/))
- Git

### 1. 克隆仓库

```bash
git clone https://gitee.com/s7w0k/pr-agent-demo.git
cd pr-agent-demo
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入真实 API Key
```

必填项：
- `DEEPSEEK_API_KEY` — DeepSeek API 密钥
- `TAVILY_API_KEY` — Tavily 搜索 API 密钥

mcp-wewe 相关变量已有内置默认值，可不填。

### 3. 开发模式启动

```bash
# Linux / macOS
make dev

# Windows PowerShell
.\scripts\dev.ps1

# Windows CMD
scripts\dev.bat
```

### 4. 访问服务

| 服务 | 地址 |
|------|------|
| Backend API | http://localhost:8000 |
| API 文档 (Swagger) | http://localhost:8000/docs |
| 前端 Dashboard | http://localhost:5173 |
| mcp-crawl 工具列表 | http://localhost:8101/tools |
| mcp-wewe 工具列表 | http://localhost:8100/tools |

---

## 常用命令

| 命令 | 说明 |
|------|------|
| `make dev` | 启动开发环境（含热重载） |
| `make up` | 启动生产环境（后台运行） |
| `make down` | 停止所有服务 |
| `make test` | 运行全部测试（pytest） |
| `make lint` | 代码检查（ruff + biome） |
| `make ci` | 本地 CI 模拟（Lint + Test） |
| `make build` | 构建 Docker 镜像 |
| `make format` | 自动格式化代码 |

---

## 目录结构

```
pr-agent-demo/
├── services/
│   ├── mcp_wewe/          # 微信公众号 RSS MCP 服务
│   ├── mcp_crawl/         # 海外安全新闻 MCP 服务
│   └── backend/           # 核心后端（FastAPI + Agent）
│       ├── api/           # REST API 路由
│       ├── agent/         # Agent 流水线（阶段二）
│       ├── models/        # MongoDB 数据模型
│       └── db/            # 数据库连接管理
├── frontend/              # React + Ant Design 仪表盘
├── mongodb/               # 数据库初始化脚本
├── docs/                  # 文档
│   └── 阶段一/            # 阶段一精细化计划
├── tests/                 # 测试
│   ├── unit/              # 单元测试（114 个用例）
│   └── integration/       # 集成测试
├── scripts/               # 运维脚本（Linux + Windows）
├── docker-compose.yml     # 容器编排
└── Makefile               # 开发命令集
```

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 运行时 | Python 3.12 / Node.js 22 |
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | MongoDB 7（Motor 异步驱动） |
| 前端 | React 18 + Ant Design 5 + Vite 6 |
| LLM | DeepSeek (chat / reasoner) |
| MCP 协议 | mcp >= 1.0（stdio JSON-RPC） |
| 容器化 | Docker + Docker Compose |
| 代码质量 | ruff (Python) + biome (TypeScript) |
| 测试 | pytest + vitest |
| CI/CD | Gitee CI (.workflow / .gitee) |

---

## 文档索引

- [技术方案与实施计划](docs/技术方案与实施计划.md)
- [智能体身份安全产品计划](docs/智能体身份安全产品计划和目标.md)
- [阶段一：精细化实施计划](docs/阶段一/基础架构搭建-精细化实施计划.md)
- [API 契约规范](docs/阶段一/API契约规范.md)

---

## 分支策略

```
main          ← 始终可部署（受保护）
  ├── feat/*  ← 功能分支 → PR 合回 main
  ├── fix/*   ← 修复分支
  └── chore/* ← 工具/构建分支
```

## Commit 规范

```
feat(scope): description
fix(scope): description
test(scope): description
chore(scope): description
ci(scope): description
docs(scope): description
```
