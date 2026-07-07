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

## CI/CD 合规规范

### 提交前必须执行的检查清单

```bash
# 1. ruff 检查（后端）
ruff check services/ tests/
ruff format --check services/ tests/

# 2. pytest 运行（模拟 CI 环境，必须 0 FAILED）
python -m pytest tests/ --cov=services --cov-report=term-missing --timeout=60 -v

# 3. 前端测试
cd frontend && npx vitest run --reporter=verbose

# 4. 推送到远程仓库
git push origin main
```

### 测试禁止事项

| 禁止 | 正确做法 |
|------|----------|
| `patch("tavily.xxx")` 依赖未安装的模块 | mock 项目内部代码，不 mock 外部不存在的模块 |
| 测试中发起真实网络请求 | `patch.object(Class, "method", ...)` mock 所有网络调用 |
| 测试依赖环境变量存在 | 测试中自行设置/清理环境变量 |
| 测试依赖文件系统特定路径 | 使用 `tmp_path` fixture 或 mock |
| 测试期望旧行为但代码已重构 | 重构代码时同步更新测试 |

### 代码重构时必须同步更新测试

- 修改 `__init__` 签名/行为 → 检查所有实例化该类的测试
- 移除依赖（如 tavily）→ 搜索测试中所有对该依赖的引用并清除
- 修改数据结构（如 SITES 配置）→ 更新断言中的字段名
- 修改 API 响应格式 → 更新 API 测试的断言

### CI 环境与本地环境差异

| CI 环境 (Linux) | 本地环境 (Windows) | 注意事项 |
|-----------------|-------------------|----------|
| Python 3.12.4 | 可能 3.13 | 版本差异可能导致行为不同 |
| 无可选模块（如 tavily） | 可能有残留 | 测试不能依赖非 requirements.txt 中的模块 |
| `timeout method: signal` | `timeout method: thread` | CI 中超时行为更严格 |
| 无 Windows Bad file descriptor | 本地有 | 本地用 `-p no:capture` 可绕过，CI 不能 |

### Git 规范

- 每次 commit 后**必须 push 到远程仓库**
- Commit message 使用 Conventional Commits 格式
- 不提交 `.env`、`node_modules/`、`__pycache__/` 等文件

## 错误沉淀记录

### #001 — CI 流水线测试失败（2026-07-07）

**错误**: 4 个 `test_mcp_crawl.py` 测试在 CI 中失败

| 测试 | 根因 | 修复 |
|------|------|------|
| `test_init_requires_api_key` | 爬虫重构为 RSS-only，`__init__` 不再校验 API key | 改为 `test_init_without_api_key` |
| `test_init_accepts_api_key` | `patch("tavily.TavilyClient")` 但 tavily 未安装 | 移除 tavily mock |
| `test_sites_configuration` | 同上 + SITES 配置结构已变（`method` → `feed`） | 检查 feed 配置 |
| `test_tools_call_without_api_key` | RSS-only 模式不报错，发起真实网络请求超时 60s | mock `NewsCrawler.crawl` |

**教训**:
1. 代码重构后必须立即检查并更新相关测试
2. 测试不能依赖未在 requirements.txt 中的模块
3. 测试不能发起真实网络请求
4. 提交前必须本地运行完整测试套件确认 0 FAILED
