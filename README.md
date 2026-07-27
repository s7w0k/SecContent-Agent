# PR Agent Demo

智能体安全 PR 情报 Agent 系统 - 从**内容爬取 -> AI 分类 -> 双维度打分 -> PR 草稿生成 -> 内容与话术检查 -> 对话改稿 -> 用户反馈与风格学习**的端到端自动化系统。

- **仓库**: https://gitee.com/s7w0k/pr-agent-demo
- **部署**: Docker Compose 一键启动
- **技术栈**: Python 3.12 / FastAPI / LangGraph / MongoDB / Redis + ARQ / React + Ant Design / DeepSeek

---

## 快速开始

### 1. 克隆并配置

```bash
git clone https://gitee.com/s7w0k/pr-agent-demo.git
cd pr-agent-demo
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY，并将 JWT_SECRET 替换为 32 字符以上随机强密钥
# 稿件内容与话术检查复用这组模型配置，不需要新增审核环境变量
```

### 2. 启动

```bash
docker compose up -d
```

### 3. 访问

浏览器打开 **http://localhost:8000**，首次使用请选择“注册”，注册完成后系统会自动登录。

> 详细部署说明见 [部署文档](docs/部署文档.md)


### 4. 首次拉取代码：分机部署构建

当前项目支持将项目主体和海外新闻爬虫部署为两个独立的 Compose Project。两台服务器均可拉取完整仓库，但分别只启动自身需要的服务；也可以先在同一台电脑上按相同方式测试。

#### 4.1 项目主体服务器

```powershell
git clone https://gitee.com/s7w0k/pr-agent-demo.git
cd pr-agent-demo
Copy-Item .env.example .env
Copy-Item deploy/local/.env.core-local.example deploy/local/.env.core-local
```

编辑 `.env`，至少配置 `JWT_SECRET`、模型 API Key、MongoDB 和 Redis 密码。编辑 `deploy/local/.env.core-local`，将 `MCP_CRAWL_URL` 设置为爬虫服务器地址，例如 `http://192.168.1.20:18101`，并设置 `MCP_CRAWL_API_KEY`。

首次构建并启动项目主体：

```powershell
docker compose -p pr-core `
  --env-file .env `
  --env-file deploy/local/.env.core-local `
  -f docker-compose.yml `
  -f deploy/core/docker-compose.remote-crawl.yml `
  -f deploy/local/docker-compose.core-local.yml `
  up -d --build
```

不要添加 `--profile embedded-crawl`，否则主体会同时启动内置爬虫。默认页面地址为 `http://项目主体服务器IP:18000`，MongoDB Compass 地址为 `mongodb://项目主体服务器IP:37017`。

#### 4.2 海外新闻爬虫服务器

```powershell
git clone https://gitee.com/s7w0k/pr-agent-demo.git
cd pr-agent-demo
Copy-Item deploy/local/.env.crawler-local.example deploy/local/.env.crawler-local
```

编辑 `deploy/local/.env.crawler-local`，配置代理和 `MCP_CRAWL_API_KEY`。该密钥必须与项目主体服务器中的值完全相同，建议使用至少 32 字节的随机 Token。

首次构建并启动独立爬虫：

```powershell
docker compose -p pr-crawler `
  --env-file deploy/local/.env.crawler-local `
  -f deploy/crawler/docker-compose.yml `
  -f deploy/crawler/docker-compose.build.yml `
  up -d --build
```

确保爬虫服务器防火墙允许项目主体服务器访问 TCP `18101` 端口。

#### 4.3 检查状态

项目主体服务器执行：

```powershell
docker compose -p pr-core `
  --env-file .env `
  --env-file deploy/local/.env.core-local `
  -f docker-compose.yml `
  -f deploy/core/docker-compose.remote-crawl.yml `
  -f deploy/local/docker-compose.core-local.yml `
  ps
```

爬虫服务器执行：

```powershell
docker compose -p pr-crawler `
  --env-file deploy/local/.env.crawler-local `
  -f deploy/crawler/docker-compose.yml `
  ps
```

所有服务应显示为 `Up` 或 `healthy`。后续更新代码时，项目主体通常只需重新构建 `backend` 和 `backend-worker`；爬虫代码没有变化时不需要重建 `pr-crawler`。完整的本地模拟、启停和故障排查说明见 [分离部署说明](deploy/local/README.md)。

---

## 核心功能

### 用户认证与多租户隔离

- 支持用户名/密码注册和 JWT 登录，刷新页面会自动恢复登录状态。
- 反馈、操作记录、用户画像、对话、草稿和流水线日志均按登录用户隔离。
- 文章和报道作为公共情报共享；个性化草稿独立保存在 `user_drafts`，不同用户互不覆盖。
- 用户菜单支持退出登录和注销账号；注销仅级联删除个人数据，不删除共享文章与报道。
- 对话和改稿流式接口使用 Query Token 认证，前端会自动附加 `?token=<JWT>`。

### V2 智能 PR 流水线

```
文章入库 -> 6分类 -> 双维度打分 -> PR 草稿生成 -> 内容与话术检查 -> 对话改稿 -> 用户反馈 -> 风格学习
```

| 阶段 | 说明 | 产品知识库 |
|------|------|-----------|
| **6分类** | LLM 将文章归入：爆点事件/法律法规/AI技术进展/竞品/行业/学术/不相关 | - |
| **筛选** | 仅前 3 类（PR 候选）进入后续流程 | - |
| **双维度打分** | 产品能力相关度 + 事件影响面与传播力（各 0-100） | ✅ 读取 `agent-security-briefs/` |
| **PR 草稿** | 综合分 ≥ 80 -> 4 篇草稿（2 套模板 × 2 个角度） | ✅ |
| **内容与话术检查** | 对照原文检查事实表述，并识别“业内第一”“领先于某公司”等高风险宣传话术，输出高/中/低三级问题与修改建议 | ✅ |
| **对话改稿** | 问答模式咨询 + 改稿模式修订，支持修订记录和应用 | ✅ |
| **用户反馈** | 对草稿/修订稿 1-5 星评分 + 文字反馈 + 标签 | ✅ |
| **风格学习** | 基于反馈和操作记录学习用户偏好，注入草稿生成 Prompt | ✅ |

流水线和单篇草稿生成通过 Redis + ARQ 提交给独立 `backend-worker` 进程：触发接口立即返回 `task_id`，前端轮询 MongoDB 中的持久化任务状态，展示 `crawl -> enrich -> classify_v2 -> filter -> score_v2 -> draft -> quality_check -> rewrite -> review` 进度。任务执行状态、LangGraph 检查点和 LLM 调用元数据均持久化；服务重启后可查询状态并从最近检查点恢复，不依赖 FastAPI 进程内内存。

### 稿件内容与话术检查

每篇最终稿生成后会自动执行检查，结果随用户草稿一起保存并显示在“对话改稿”页面：

1. **事实内容**：仅根据已入库的原文核对稿件中的数据、时间、主体、因果关系和归属表述；缺少原文时会明确标记事实检查不完整。
2. **宣传话术**：识别“业内第一”“唯一”“遥遥领先”“超过/强于某公司”等缺少充分支撑或容易造成误导的表达。
3. **分级建议**：每个问题标记高、中、低等级，展示原句、问题说明和可直接采用的修改建议；系统只输出内容问题，不给出法务确认或可发布结论。
4. **状态与重检**：检查失败不会阻断草稿保存；内容变更后旧结果会标记为已过期。用户可手动重新检查，应用修订稿后系统会自动重检。

检查功能复用主体服务 `.env` 中的 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`，不需要规则 YAML、公司敏感配置或额外审核服务。

### 多租户自定义 PR 模板

登录后点击顶部菜单 **“PR 模板”**，可以按账号维护三个 PR 分类下的六套 A/B 模板：

1. 选择“爆点事件”“法律法规 / 监管”或“AI 技术重大进展”。
2. 点击模板卡片的“编辑”，修改模板名称、标题骨架、章节、两个生成视角和补充要求。
3. 章节支持新增、删除、拖拽及上下移动；“预览骨架”只渲染 Markdown，不调用 LLM。
4. 保存后生成用户独立版本；后续 V2 流水线和单篇生成会冻结本次任务使用的模板版本。
5. “历史”可查看并恢复旧版本，恢复操作会创建新版本；“恢复默认”仅停用当前用户覆盖，不影响其他用户。

卡片上的“系统默认 / 用户自定义”、版本号和更新时间表示当前账号实际生效的模板。编辑未保存时，关闭抽屉、切换分类或离开页面都会弹出确认提示。

### 用户文件上传

登录后可在“仪表盘”点击 **“上传文章”**，把本地材料直接加入文章库：

1. 支持 `.txt`、`.md`、`.pdf`、`.docx`，单个文件最大 **10 MB**。
2. 标题可选；未填写时使用文件名。TXT 会自动兼容 UTF-8/GBK，DOCX 标题样式会转换成 Markdown 标题。
3. 前端会先拦截不支持的扩展名和超大文件；加密 PDF、损坏文件、空内容及同一用户重复内容会显示后端返回的明确错误。
4. 上传成功后文章按最新入库时间显示，来源为紫色“用户上传”标签，也可用来源筛选器单独查看。
5. 上传文章与爬取文章共用分类、打分和草稿生成流水线，不需要启动或修改独立爬虫。

### 自定义初稿提示词

登录后点击顶部 **“配置” -> “初稿生成提示词”**，可以查看当前系统提示词并保存当前账号专属版本。自定义内容必须完整保留以下占位符：

- `{knowledge_context}`：产品知识和文章上下文。
- `{template_spec}`：本次任务冻结的 PR 模板要求。
- `{style_hints}`：当前用户的风格偏好。

缺失占位符时前后端都会拒绝保存。保存成功后状态变为“已自定义”，后续初稿生成和重写自动使用该版本；“恢复系统默认”会在二次确认后删除当前用户的覆盖。配置按用户隔离，编辑未保存时切换菜单会提示是否放弃修改。

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
4. **内容与话术检查** - 查看事实内容和宣传话术问题、风险等级与修改建议，也可手动重新检查
5. **修订记录** - 左栏底部显示历史修订记录，可点击查看任意版本
6. **应用修订** - 选择满意的修订稿，点击"应用为当前稿"将其设为主稿并自动重检
7. **复制/下载** - 修订稿支持复制到剪贴板和下载 `.md` 文件

### 用户反馈与风格学习

系统会自动记录用户的操作行为（查看草稿、下载草稿、改稿、应用修订等），并支持对草稿和修订稿进行评分反馈。基于这些数据，系统会学习用户的风格偏好，逐步优化后续草稿生成。

1. **草稿反馈** - 在草稿查看器或改稿结果页，对草稿进行 1-5 星评分，可附加文字反馈和预设标签
2. **操作记录** - 下载草稿、应用修订、触发流水线等操作自动记录到后台
3. **风格画像** - 点击菜单"用户画像"查看风格偏好（偏好模板/视角/语气/篇幅/改稿方向）
4. **重建画像** - 在用户画像页面点击"重建画像"，基于最新反馈数据重新生成风格画像
5. **偏好注入** - 反馈累计 ≥ 5 条后自动生成画像，新生成的草稿会参考用户偏好
6. **打分微调** - 对文章打分的反馈（偏高/偏低）会微调后续打分阈值（±10 分上限）

### 开发者日志

系统将流水线、对话改稿、反馈、画像和认证操作写入 MongoDB `pipeline_logs`，并通过同一 `trace_id` 关联一次调用的完整链路。普通用户只能查看自己的运行日志；开发者可以跨用户排查全链路问题。

先注册开发者账号，再授予开发者权限：

```bash
# 参数支持 user_id 或 username
docker compose exec backend python scripts/set_developer.py alice
```

刷新页面后，顶部菜单会显示“开发者日志”。页面支持：

- 按日期、用户、阶段、级别、Trace ID 和消息关键词筛选。
- 分页查看所有用户的日志，ERROR/CRITICAL 事件红色高亮。
- 点击日志行展开 `detail` 和结构化错误信息。
- 点击 Trace ID 查看按时间排序的完整链路、阶段数、总耗时和异常状态。

如需撤销权限：

```bash
docker compose exec backend python scripts/set_developer.py alice --disable
```

> 开发者日志可能包含跨用户运行元数据，仅应向受信任的排障人员授权。权限变更后刷新页面或重新登录即可生效。

### 产品知识库可视化管理

系统提供产品知识库的可视化浏览与管理功能，位于「配置 → 产品知识库」菜单下。

**全员浏览**：所有登录用户可查看知识库真实目录树、Markdown 正文、文件用途标签和评分参与状态。

**管理员维护**：`is_admin=true` 的用户可创建草稿、校验、Prompt 预览、试打分、发布和回滚。

```text
浏览目录 -> 创建草稿 -> 校验格式 -> 预览Prompt变化 -> 试打分对比 -> 发布到正式文件 -> 回滚（如需要）
```

核心特性：

- 正式知识仍存放在 `agent-security-briefs/` 目录，MongoDB 仅存储草稿和发布历史。
- 发布采用原子写入（`os.replace`），失败自动恢复。
- 发布后 API 进程立即刷新知识，Worker 在下一个任务开始前检测文件变更。
- 5 个核心打分文件受保护，允许编辑内容但禁止重命名和删除。
- API 容器知识目录为读写挂载（`:rw`），Worker 保持只读（`:ro`）。

设置管理员权限：

```bash
docker compose exec backend python -c "
from db.mongo import MongoDB; import asyncio
async def main():
    await MongoDB.connect(uri='mongodb://admin:pr_agent_2024@mongodb:27017', db_name='pr_agent')
    await MongoDB.get_collection('users').update_one({'username': 'admin'}, {'\$set': {'is_admin': True}})
    await MongoDB.disconnect()
asyncio.run(main())
"
```

---

## 架构

```
docker compose up
├── mongodb:27017      - 业务数据、任务状态、LangGraph 检查点
├── redis:6379         - ARQ 持久任务队列（AOF）
├── mcp-crawl:8101     - 海外安全新闻 RSS 爬虫
├── mcp-wewe:8100      - 微信公众号 RSS 桥接（可选）
├── backend:8000       - FastAPI API + React 前端，只负责入队与查询
└── backend-worker     - 独立执行 Agent 流水线和检查点恢复
```

### V2 Agent 模块

```
services/backend/agent/
├── classifier_v2.py    # 6分类（LLM 判断关联性 + 归类）
├── scorer_v2.py        # 双维度打分（产品相关度 + 事件影响力）
├── pr_templates.py     # 6 套 PR 模板（3 类 × 2 套）
├── template_repository.py # 多租户模板覆盖、版本、回滚与默认回退
├── draft_generator.py  # 草稿生成器（每文 4 稿，支持风格偏好注入）
├── draft_reviewer.py   # 稿件事实内容与高风险宣传话术检查
├── draft_chat.py       # 对话改稿 Agent（问答 + 改稿）
├── style_profiler.py   # 风格画像管理 Agent（偏好提取 + 画像构建）
├── pipeline_v2.py      # LangGraph 条件路由流水线编排
├── pipeline_state.py   # MongoDB 持久化任务状态
├── checkpointer.py     # LangGraph MongoDB 检查点
├── task_queue.py       # ARQ 任务定义与 Worker 配置
├── llm_wrapper.py      # 结构化输出降级与 LLM 调用观测
├── schemas.py          # 分类、评分结构化输出 Schema
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
├── tests/                   # 后端与前端自动化测试（800+ 用例）
├── docs/                    # 文档
│   └── 部署文档.md
└── docker-compose.yml       # 容器编排
```

---

## 常用命令

```bash
docker compose up -d                  # 启动
docker compose down                   # 停止
docker compose logs backend -f        # 查看 API 日志
docker compose logs backend-worker -f # 查看 Worker 执行日志
docker compose build --no-cache backend backend-worker  # 重建 API 与 Worker
docker compose exec backend-worker python -m arq worker.WorkerSettings --check  # Worker 心跳

make test                             # 运行全部测试
make lint                             # 代码检查
```

---

## API 端点

| 端点 | 说明 |
|------|------|
| `POST /api/auth/register` | 注册用户 |
| `POST /api/auth/login` | 登录并获取 JWT |
| `GET /api/auth/me` | 获取当前登录用户 |
| `DELETE /api/auth/account` | 注销账号并删除个人数据 |
| `POST /api/pipeline/run-v2` | 创建 V2 全流程后台任务，返回 `task_id` |
| `POST /api/pipeline/run-v2/{hash}` | 创建单篇 V2 后台任务，返回 `task_id` |
| `GET /api/pipeline/status-v2?task_id={task_id}` | 查询当前用户的 V2 持久化状态与节点进度 |
| `GET /api/pipeline/tasks/{task_id}` | 查询当前用户任务状态与进度 |
| `GET /api/pipeline/tasks` | 查询当前用户任务列表 |
| `GET /api/pipeline/tasks/{task_id}/checkpoints` | 查询当前用户任务的 LangGraph 检查点 |
| `POST /api/pipeline/tasks/{task_id}/resume` | 从最近检查点重新入队恢复任务 |
| `GET /api/llm-logs` | 查询当前用户的 LLM 调用元数据 |
| `POST /api/pipeline/classify-v2` | V2 6分类 |
| `POST /api/pipeline/score-v2` | V2 双维度打分（批量） |
| `POST /api/pipeline/score-v2/{hash}` | V2 单篇打分 |
| `POST /api/pipeline/crawl-overseas` | 爬取海外新闻 |
| `GET /api/articles` | 文章列表 |
| `GET /api/articles/hot` | 热点排行（兼容历史评分和时间格式） |
| `GET /api/articles/{url_hash}` | 文章详情 |
| `POST /api/chat/ask` | 对话问答（文章/草稿上下文） |
| `POST /api/chat/ask_stream?token={jwt}` | SSE 流式对话 |
| `POST /api/articles/{url_hash}/drafts/{draft_index}/revise` | 改稿（生成修订稿） |
| `POST /api/articles/{url_hash}/drafts/{draft_index}/revise_stream?token={jwt}` | SSE 流式改稿 |
| `POST /api/articles/{url_hash}/drafts/{draft_index}/review` | 手动重新检查稿件内容与宣传话术 |
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
| `GET /api/pr-templates` | 查询当前用户六套有效模板；支持 `category_v2` 筛选 |
| `GET /api/pr-templates/{template_key}` | 查询当前用户单套有效模板 |
| `PUT /api/pr-templates/{template_key}` | 保存用户模板覆盖，支持 `expected_version` 乐观锁 |
| `POST /api/pr-templates/{template_key}/preview` | 生成 Markdown 骨架预览，不调用 LLM |
| `POST /api/pr-templates/{template_key}/reset` | 恢复当前用户的系统默认模板 |
| `GET /api/pr-templates/{template_key}/versions` | 分页查询当前用户模板历史 |
| `POST /api/pr-templates/{template_key}/versions/{version}/restore` | 将历史快照恢复为新版本 |
| `GET /api/dev/logs` | 开发者跨用户日志查询（筛选+分页） |
| `GET /api/dev/logs/dates` | 开发者日志日期列表 |
| `GET /api/dev/logs/trace/{trace_id}` | 开发者查看完整 Trace 链路 |
| `GET /api/dev/logs/stats` | 开发者日志统计 |
| `GET /api/health` | 健康检查 |

---

## MongoDB 集合

| 集合 | 说明 |
|------|------|
| `users` | 用户账号和 bcrypt 密码摘要 |
| `articles` | 共享文章数据 |
| `reports` | V1 PR 报道 |
| `chat_sessions` | 按用户隔离的对话改稿历史 |
| `user_drafts` | 按用户和文章隔离的个性化草稿 |
| `feedbacks` | 用户反馈记录（评分+文字+标签） |
| `user_activities` | 用户操作记录（下载/改稿/应用等） |
| `user_profiles` | 用户风格画像（偏好模板/视角/语气等） |
| `user_pr_templates` | 当前用户对六套系统模板的有效覆盖；按 `user_id + template_key` 唯一 |
| `user_pr_template_versions` | 用户模板不可变历史快照；按用户、模板和版本隔离，最多保留 20 版 |
| `pipeline_tasks` | 异步流水线任务状态和结果 |
| `pipeline_checkpoints` | LangGraph 节点级状态快照 |
| `pipeline_checkpoint_writes` | LangGraph 检查点中间写入 |
| `llm_call_logs` | 结构化输出、降级原因、Token 和耗时等 LLM 调用元数据 |
| `pipeline_logs` | 含用户归属和 Trace ID 的全链路日志；普通用户隔离、开发者可跨用户查询 |

---

## Commit 规范

```
feat: description    # 新功能
fix: description     # 修复
test: description    # 测试
docs: description    # 文档
chore: description   # 杂项
```
