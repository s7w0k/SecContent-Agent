# PR Agent Demo

智能体安全 PR 情报 Agent 系统 — 从**内容爬取 → AI 分类 → 双维度打分 → PR 草稿生成 → 可视化展示**的端到端自动化系统。

- **仓库**: https://gitee.com/s7w0k/pr-agent-demo
- **部署**: Docker Compose 一键启动
- **技术栈**: Python 3.12 / FastAPI / LangChain / MongoDB / React + Ant Design / DeepSeek

---

## 快速开始

### 1. 克隆并配置

```bash
git clone https://gitee.com/s7w0k/pr-agent-demo.git
cd pr-agent-demo
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
```

### 2. 启动

```bash
docker compose up -d
```

### 3. 访问

浏览器打开 **http://localhost:8000**

> 详细部署说明见 [部署文档](docs/部署文档.md)

---

## 核心功能

### V2 智能 PR 流水线

```
文章入库 → 6分类 → 双维度打分 → PR 草稿生成
```

| 阶段 | 说明 | 产品知识库 |
|------|------|-----------|
| **6分类** | LLM 将文章归入：爆点事件/法律法规/AI技术进展/竞品/行业/学术/不相关 | — |
| **筛选** | 仅前 3 类（PR 候选）进入后续流程 | — |
| **双维度打分** | 产品能力相关度 + 事件影响面与传播力（各 0-100） | ✅ 读取 `agent-security-briefs/` |
| **PR 草稿** | 综合分 ≥ 80 → 4 篇草稿（2 套模板 × 2 个角度） | ✅ |

### 数据源

| 来源 | 方式 | 状态 |
|------|------|------|
| 海外安全新闻 | RSS Feed（5 站点，无需 API Key） | ✅ |
| 微信公众号 | WeWe RSS | 可选 |

---

## 架构

```
docker compose up
├── mongodb:27017      — 数据持久化
├── mcp-crawl:8101     — 海外安全新闻 RSS 爬虫
├── mcp-wewe:8100      — 微信公众号 RSS 桥接（可选）
└── backend:8000       — FastAPI + LangGraph Agent + React 前端
```

### V2 Agent 模块

```
services/backend/agent/
├── classifier_v2.py    # 6分类（LLM 判断关联性 + 归类）
├── scorer_v2.py        # 双维度打分（产品相关度 + 事件影响力）
├── pr_templates.py     # 6 套 PR 模板（3 类 × 2 套）
├── draft_generator.py  # 草稿生成器（每文 4 稿）
├── pipeline_v2.py      # LangGraph 流水线编排
└── knowledge.py        # 产品知识库加载器（V2 多文件 + CLAUDE.md 市场角色）
```

---

## 目录结构

```
pr-agent-demo/
├── agent-security-briefs/   # 产品知识库（给 LLM 打分用）
├── services/
│   ├── backend/             # 核心后端
│   │   ├── agent/           # Agent 流水线
│   │   ├── api/             # REST API 路由
│   │   ├── models/          # MongoDB 数据模型
│   │   └── db/              # 数据库连接
│   ├── mcp_crawl/           # 海外新闻 RSS 爬虫
│   └── mcp_wewe/            # 微信公众号 MCP 服务
├── frontend/                # React + Ant Design 仪表盘
├── tests/                   # 测试（149+ 用例）
├── docs/                    # 文档
│   └── 部署文档.md
└── docker-compose.yml       # 容器编排
```

---

## 常用命令

```bash
docker compose up -d                  # 启动
docker compose down                   # 停止
docker compose logs backend -f        # 查看日志
docker compose build --no-cache backend  # 重建后端

make test                             # 运行全部测试
make lint                             # 代码检查
```

---

## API 端点

| 端点 | 说明 |
|------|------|
| `POST /api/pipeline/run-v2` | 触发 V2 全流程（批量） |
| `POST /api/pipeline/run-v2/{hash}` | 单篇 V2 流水线 |
| `POST /api/pipeline/classify-v2` | V2 6分类 |
| `POST /api/pipeline/score-v2` | V2 双维度打分（批量） |
| `POST /api/pipeline/score-v2/{hash}` | V2 单篇打分 |
| `POST /api/pipeline/crawl-overseas` | 爬取海外新闻 |
| `GET /api/articles` | 文章列表 |
| `GET /api/health` | 健康检查 |

---

## Commit 规范

```
feat: description    # 新功能
fix: description     # 修复
test: description    # 测试
docs: description    # 文档
chore: description   # 杂项
```
