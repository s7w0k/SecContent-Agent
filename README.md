# PR Agent Demo

智能体安全 PR 情报 Agent 系统 - 从**内容爬取 -> AI 分类 -> 双维度打分 -> PR 草稿生成 -> 对话改稿 -> 用户反馈与风格学习**的端到端自动化系统。

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
文章入库 -> 6分类 -> 双维度打分 -> PR 草稿生成 -> 对话改稿 -> 用户反馈 -> 风格学习
```

| 阶段 | 说明 | 产品知识库 |
|------|------|-----------|
| **6分类** | LLM 将文章归入：爆点事件/法律法规/AI技术进展/竞品/行业/学术/不相关 | - |
| **筛选** | 仅前 3 类（PR 候选）进入后续流程 | - |
| **双维度打分** | 产品能力相关度 + 事件影响面与传播力（各 0-100） | ✅ 读取 `agent-security-briefs/` |
| **PR 草稿** | 综合分 ≥ 80 -> 4 篇草稿（2 套模板 × 2 个角度） | ✅ |
| **对话改稿** | 问答模式咨询 + 改稿模式修订，支持修订记录和应用 | ✅ |
| **用户反馈** | 对草稿/修订稿 1-5 星评分 + 文字反馈 + 标签 | ✅ |
| **风格学习** | 基于反馈和操作记录学习用户偏好，注入草稿生成 Prompt | ✅ |

### 数据源

| 来源 | 方式 | 状态 |
|------|------|------|
| 海外安全新闻 | RSS Feed（5 站点，无需 API Key） | ✅ |
| 微信公众号 | WeWe RSS | 可选 |

### 对话改稿工作台

浏览器打开 http://localhost:8000，点击左侧菜单"对话改稿"进入工作台：

1. **选择文章和草稿** - 左栏选择有 PR 草稿的文章，切换查看 4 篇草稿
2. **问答模式** - 输入问题，AI 基于文章和草稿上下文回答（如"这个标题够不够吸引人？"）
3. **改稿模式** - 输入修改意见，AI 生成修订稿并自动保存（如"标题更有冲击力，减少技术细节"）
4. **修订记录** - 左栏底部显示历史修订记录，可点击查看任意版本
5. **应用修订** - 选择满意的修订稿，点击"应用为当前稿"将其设为主稿
6. **复制/下载** - 修订稿支持复制到剪贴板和下载 `.md` 文件

### 用户反馈与风格学习

系统会自动记录用户的操作行为（查看草稿、下载草稿、改稿、应用修订等），并支持对草稿和修订稿进行评分反馈。基于这些数据，系统会学习用户的风格偏好，逐步优化后续草稿生成。

1. **草稿反馈** - 在草稿查看器或改稿结果页，对草稿进行 1-5 星评分，可附加文字反馈和预设标签
2. **操作记录** - 下载草稿、应用修订、触发流水线等操作自动记录到后台
3. **风格画像** - 点击菜单"用户画像"查看风格偏好（偏好模板/视角/语气/篇幅/改稿方向）
4. **重建画像** - 在用户画像页面点击"重建画像"，基于最新反馈数据重新生成风格画像
5. **偏好注入** - 反馈累计 ≥ 5 条后自动生成画像，新生成的草稿会参考用户偏好
6. **打分微调** - 对文章打分的反馈（偏高/偏低）会微调后续打分阈值（±10 分上限）

---

## 架构

```
docker compose up
├── mongodb:27017      - 数据持久化
├── mcp-crawl:8101     - 海外安全新闻 RSS 爬虫
├── mcp-wewe:8100      - 微信公众号 RSS 桥接（可选）
└── backend:8000       - FastAPI + LangGraph Agent + React 前端
```

### V2 Agent 模块

```
services/backend/agent/
├── classifier_v2.py    # 6分类（LLM 判断关联性 + 归类）
├── scorer_v2.py        # 双维度打分（产品相关度 + 事件影响力）
├── pr_templates.py     # 6 套 PR 模板（3 类 × 2 套）
├── draft_generator.py  # 草稿生成器（每文 4 稿，支持风格偏好注入）
├── draft_chat.py       # 对话改稿 Agent（问答 + 改稿）
├── style_profiler.py   # 风格画像管理 Agent（偏好提取 + 画像构建）
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
├── tests/                   # 测试（180+ 用例）
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
| `GET /api/articles/{url_hash}` | 文章详情 |
| `POST /api/chat/ask` | 对话问答（文章/草稿上下文） |
| `POST /api/articles/{url_hash}/drafts/{draft_index}/revise` | 改稿（生成修订稿） |
| `POST /api/articles/{url_hash}/drafts/{draft_index}/revisions/{revision_id}/apply` | 应用修订为当前稿 |
| `POST /api/feedback` | 提交反馈（评分+文字+标签） |
| `GET /api/feedback` | 查询反馈列表（支持筛选） |
| `GET /api/feedback/stats` | 反馈统计（按模板/视角分组） |
| `PUT /api/feedback/{feedback_id}` | 更新反馈 |
| `DELETE /api/feedback/{feedback_id}` | 删除反馈 |
| `POST /api/activities/log` | 记录单条用户操作 |
| `POST /api/activities/batch-log` | 批量记录用户操作 |
| `GET /api/activities` | 查询操作记录（分页+筛选） |
| `GET /api/activities/stats` | 操作统计（按类型/模板/日期分组） |
| `GET /api/profile/style` | 获取用户风格画像 |
| `POST /api/profile/rebuild` | 重建用户风格画像 |
| `GET /api/health` | 健康检查 |

---

## MongoDB 集合

| 集合 | 说明 |
|------|------|
| `articles` | 文章数据（含 PR 草稿、修订记录、反馈冗余） |
| `reports` | V1 PR 报道 |
| `chat_sessions` | 对话改稿历史 |
| `feedbacks` | 用户反馈记录（评分+文字+标签） |
| `user_activities` | 用户操作记录（下载/改稿/应用等） |
| `user_profiles` | 用户风格画像（偏好模板/视角/语气等） |

---

## Commit 规范

```
feat: description    # 新功能
fix: description     # 修复
test: description    # 测试
docs: description    # 文档
chore: description   # 杂项
```
