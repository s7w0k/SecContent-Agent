# 阶段二：Agent 核心流水线 — 精细化实施计划

> **上游文档**: [技术方案与实施计划](../技术方案与实施计划.md)
> **前置阶段**: [阶段一：基础架构搭建](../阶段一/基础架构搭建-精细化实施计划.md)
> **仓库地址**: https://gitee.com/s7w0k/pr-agent-demo
> **交付周期**: Day 3-4（约 16 工时）
> **版本**: v1.0 | 2026-06-29

---

## 目录

1. [整体目标与交付物](#1-整体目标与交付物)
2. [与阶段一的衔接](#2-与阶段一的衔接)
3. [技术选型与依赖](#3-技术选型与依赖)
4. [Agent 流水线详细设计](#4-agent-流水线详细设计)
5. [测试策略](#5-测试策略)
6. [任务精细化拆解](#6-任务精细化拆解)
7. [Day-by-Day 执行计划](#7-day-by-day-执行计划)
8. [验收标准](#8-验收标准)

---

## 1. 整体目标与交付物

### 1.1 目标

在阶段一的骨架之上，实现 Agent 核心流水线：通过 LangGraph 编排 MCP 工具调用 → LLM 打分 → 报道生成，打通**爬取 → 入库 → 分类 → 打分 → 报道**全链路。

### 1.2 交付物清单

| # | 交付物 | 路径 | 验收方式 |
|---|--------|------|----------|
| D1 | MCP 工具集（LangChain Tool 包装） | `services/backend/agent/tools.py` | 每个工具可独立调用，返回结构化结果 |
| D2 | 产品知识库加载器 | `services/backend/agent/knowledge.py` | 加载产品文档，输出结构化知识摘要 |
| D3 | 双维度打分 Agent | `services/backend/agent/scorer.py` | 单篇文章打分耗时 < 5s，输出 0-100 两维分数 |
| D4 | PR 报道生成 Agent | `services/backend/agent/reporter.py` | 按模板生成结构化 Markdown 报道 |
| D5 | LangGraph 流水线编排 | `services/backend/agent/pipeline.py` | `POST /api/pipeline/run` 全流程执行成功 |
| D6 | 流水线 REST API | `services/backend/api/pipeline.py` | 4 个端点：全流程 / 单阶段 / 状态查询 |
| D7 | 仪表盘数据 API | `services/backend/api/dashboard.py` | 文章列表 + 统计 + 筛选 + 分页 |
| D8 | 单元测试 | `tests/unit/` | ≥ 80% 行覆盖率，mock LLM 调用 |

---

## 2. 与阶段一的衔接

### 2.1 阶段一已就绪的基础设施

```
┌─────────────────────────────────────────────────────────┐
│ 阶段一已完成                                             │
│                                                         │
│  ✅ MongoDB 连接层 (db/mongo.py)                        │
│  ✅ Article / Report 数据模型 (models/)                 │
│  ✅ mcp-wewe HTTP Bridge → localhost:8100               │
│  ✅ mcp-crawl HTTP Bridge → localhost:8101              │
│  ✅ Backend FastAPI 入口 (main.py)                      │
│  ✅ Docker Compose 四服务编排                            │
│  ✅ CI/CD 流水线 + 本地 CI 脚本                          │
│  ✅ 114 个测试用例 + pytest 配置                         │
└─────────────────────────────────────────────────────────┘
```

### 2.2 阶段二新增内容

```
┌─────────────────────────────────────────────────────────┐
│ 阶段二新增                                               │
│                                                         │
│  🆕 agent/tools.py      — MCP Tool → LangChain Tool     │
│  🆕 agent/knowledge.py  — 产品知识库加载器               │
│  🆕 agent/scorer.py     — 双维度打分 Agent               │
│  🆕 agent/reporter.py   — PR 报道生成 Agent              │
│  🆕 agent/pipeline.py   — LangGraph 流水线编排           │
│  🆕 api/pipeline.py     — 流水线 REST API                │
│  🆕 api/dashboard.py    — 仪表盘数据 API                 │
│  🆕 api/reports.py      — PR 报道 CRUD API               │
│  🆕 单元测试 + Mock LLM                                  │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 技术选型与依赖

### 3.1 新增依赖

```text
# services/backend/requirements.txt 新增项

# ── LangChain 生态 ───────────────────────────────────
langchain>=0.3.0
langchain-core>=0.3.0
langgraph>=0.2.0
langchain-openai>=0.3.0

# ── MCP Client ──────────────────────────────────────
mcp>=1.0.0
httpx>=0.27.0

# ── 已有（阶段一）───────────────────────────────────
fastapi>=0.115.0
motor>=3.7.0
pydantic>=2.10.0
pydantic-settings>=2.6.0
```

### 3.2 LLM 配置

| 参数 | 值 | 说明 |
|------|-----|------|
| Provider | DeepSeek | 统一 LLM 提供商 |
| Model（打分/报道） | `deepseek-chat` | 性价比最高 |
| Model（复杂推理） | `deepseek-reasoner` | 特殊情况可选 |
| API Base | `https://api.deepseek.com` | |
| Temperature（打分） | 0.1 | 低温度确保一致性 |
| Temperature（报道） | 0.4 | 适度创造性 |
| Max Tokens（报道） | 4096 | 报道内容较长 |

### 3.3 关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| Agent 框架 | LangGraph StateGraph | 多阶段流水线天然适合状态图编排，比 ReAct 更可控 |
| Tool 封装方式 | `@tool` 装饰器 + httpx 异步调用 | MCP 服务通过 HTTP 暴露，用 httpx 调用比 MCP SDK 更轻量 |
| LLM 调用方式 | `ChatDeepSeek` (langchain-openai 兼容) | DeepSeek API 兼容 OpenAI 格式，直接用 langchain-openai |
| 知识库加载 | 启动时加载一次，内存缓存 | 避免每次请求读取文件；产品文档不大（< 50KB） |
| 并发控制 | asyncio.Semaphore(3) | 限制并发 LLM 调用，避免 API 限流 |

---

## 4. Agent 流水线详细设计

### 4.1 LangGraph 状态图

```python
# services/backend/agent/pipeline.py

from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END

class PipelineState(TypedDict):
    """流水线全局状态"""
    # 输入
    crawl_days: int
    phases: list[str]              # ["crawl", "classify", "score", "report"]

    # 中间结果
    crawled_count: int
    classified_count: int
    scored_count: int
    report_count: int

    # 错误
    errors: list[str]
    status: str                    # "idle" | "running" | "completed" | "failed"
    current_phase: str

# 构建图
graph = StateGraph(PipelineState)

graph.add_node("crawl", crawl_node)
graph.add_node("classify", classify_node)
graph.add_node("score", score_node)
graph.add_node("report", report_node)

graph.set_entry_point("crawl")
graph.add_edge("crawl", "classify")
graph.add_edge("classify", "score")
graph.add_edge("score", "report")
graph.add_edge("report", END)

pipeline = graph.compile()
```

### 4.2 各阶段节点详解

#### crawl_node — 爬取阶段

```
输入: crawl_days (int)
执行:
  1. 调用 mcp-wewe: POST /fetch-yesterday   → 获取公众号文章
  2. 调用 mcp-crawl: POST /crawl-news        → 获取海外新闻
  3. 对每篇文章去重（url_hash）
  4. 存入 MongoDB articles collection（status: "crawled"）
  5. 对每篇调用 fetch_article_fulltext 获取全文（仅 wewe）
输出: crawled_count, 更新 PipelineState
```

#### classify_node — 分类阶段

```
输入: 刚爬取的文章列表
执行:
  1. 从 MongoDB 读取 status="crawled" 的文章
  2. 调用 mcp-crawl: POST /classify → AI 分类
  3. 更新 MongoDB: 设置 is_ai_security, is_agent_security, category
  4. 更新 status → "classified"
输出: classified_count, ai_security_count
```

#### score_node — 打分阶段

```
输入: 已分类的 AI 安全相关文章
执行:
  1. 从 MongoDB 读取 status="classified" AND is_ai_security=true
  2. 加载知识库（产品定位、技术壁垒、控标点）
  3. 对每篇文章调用 LLM 打分:
     - System Prompt: 知识库 + 打分维度说明
     - User Prompt: 文章标题 + 摘要 + 分类结果
     - 输出: {ai_relevance_score, reportability_score, score_reason}
  4. 并发控制: asyncio.Semaphore(3)
  5. 更新 MongoDB: 写入分数，status → "scored"
  6. total_score ≥ 140 标记为 is_high_value
输出: scored_count, high_value_count
```

#### report_node — 报道生成阶段

```
输入: 高分文章列表
执行:
  1. 从 MongoDB 读取 status="scored" AND total_score≥140
  2. 对每篇调用 LLM 生成报道:
     - System Prompt: 知识库 + PR 报道模板
     - User Prompt: 文章全文 + 分数详情
     - 输出: 结构化 Markdown 报道
  3. 存入 MongoDB reports collection
  4. 更新 articles: has_report=true, report_id
  5. 更新 status → "completed"
输出: report_count
```

### 4.3 知识库设计

```python
# services/backend/agent/knowledge.py

@dataclass
class ProductKnowledge:
    """产品知识库结构"""
    product_name: str
    product_positioning: str       # 产品定位
    core_features: list[str]       # 6 大独创功能
    tech_barriers: list[str]       # 技术壁垒
    control_points: list[str]      # 16 项控标点
    customer_cases: list[str]      # 客户案例
    competitors: list[str]         # 竞品列表
    target_industries: list[str]   # 目标行业

class KnowledgeLoader:
    """从 docs/ 加载产品知识"""

    def __init__(self, docs_dir: str = "/app/docs"):
        self.docs_dir = docs_dir
        self._cache: Optional[ProductKnowledge] = None

    async def load(self) -> ProductKnowledge:
        """加载并缓存知识库（启动时调用一次）"""
        if self._cache:
            return self._cache
        # 读取 docs/智能体身份安全产品计划和目标.md
        # LLM 提取结构化信息
        self._cache = await self._extract_knowledge()
        return self._cache

    def as_system_prompt(self) -> str:
        """将知识库转换为 System Prompt 片段"""
        # 返回精简版知识摘要，插入到打分/报道 Agent 的 System Prompt
```

### 4.4 打分 Prompt 设计

```text
SYSTEM:
你是一个智能体安全领域的技术情报分析师。

## 产品背景
{product_knowledge}

## 打分维度
1. **AI/Agent安全相关度** (0-100):
   - 90-100: 直接涉及智能体身份安全核心领域（身份认证、权限管控、意图防护、MCP协议安全）
   - 70-89: 相关领域重要事件（模型安全、数据安全、AI供应链）
   - 40-69: 泛安全领域有一定关联
   - 0-39: 基本不相关

2. **可报道性** (0-100):
   - 90-100: 重大漏洞披露/新技术突破/行业标志性事件
   - 70-89: 有分析价值的趋势/竞品重要动态
   - 40-69: 常规安全新闻
   - 0-39: 日常报道，无明显新闻价值

## 输出格式
{ "ai_relevance_score": int, "reportability_score": int, "score_reason": "string", "tags": ["tag1"] }

USER:
请对以下文章打分：
标题: {title}
来源: {source}
摘要: {summary}
分类: {category}
```

### 4.5 PR 报道 Prompt 设计

```text
SYSTEM:
你是一个智能体安全行业的技术PR撰稿人。
请根据提供的文章内容和产品背景，撰写一篇面向公司内部的产品PR情报报道。

## 产品背景
{product_knowledge}

## 报道模板
# [{title}]

## 导语
[2-3句话概述事件核心，突出与智能体身份安全的关联]

## 背景
[事件背景、涉及厂商/技术、行业上下文]

## 分析
[结合公司产品能力的技术分析，为什么这对我们重要]

## 影响评估
[对行业/客户/竞品/公司产品的潜在影响]

## 行动建议
[产品侧可采取的应对建议，关联具体功能模块]

USER:
请基于以下文章生成PR报道：
{article_full_content}
分数: AI相关度 {ai_score}/100, 可报道性 {reportability}/100
打分理由: {score_reason}
```

### 4.6 REST API 设计

```python
# services/backend/api/pipeline.py

# POST /api/pipeline/run
#   请求: {"crawl_days": 1, "phases": ["crawl","classify","score","report"]}
#   响应: {"pipeline_id": "uuid", "status": "running", "started_at": "..."}

# POST /api/pipeline/crawl
#   请求: {"crawl_days": 1}
#   响应: {"ok": true, "pipeline_id": "uuid", "crawled_count": N}

# POST /api/pipeline/score
#   请求: {"article_url_hashes": ["hash1", ...]}  # 可选，默认对所有 classified 文章打分
#   响应: {"ok": true, "scored_count": N}

# POST /api/pipeline/report
#   请求: {"article_url_hashes": ["hash1", ...]}  # 可选，默认对所有 ≥140 分文章生成报道
#   响应: {"ok": true, "report_count": N}

# GET /api/pipeline/status
#   响应: {"status": "completed", "current_phase": "report", "progress": {...}}
```

```python
# services/backend/api/dashboard.py

# GET /api/articles
#   ?page=1&page_size=20
#   &source_type=overseas_news|wechat_mp
#   &category=MCP协议漏洞
#   &min_score=100
#   &date_from=2026-06-01
#   &sort_by=added_at&order=desc
#   响应: {"items": [...], "total": 150, "page": 1, "page_size": 20}

# GET /api/articles/{url_hash}
#   响应: 单篇文章详情（含 content_md 原文全文）

# GET /api/stats
#   响应: {
#     "total_articles": 150,
#     "ai_security_count": 45,
#     "high_value_count": 12,
#     "source_distribution": {"overseas_news": 80, "wechat_mp": 70},
#     "category_distribution": {"MCP协议漏洞": 15, "提示注入": 10, ...},
#     "score_distribution": {"0-30": 50, "31-60": 40, "61-100": 30, "101-140": 18, "141-200": 12},
#     "recent_trend": [...]   # 最近 7 天每日新增
#   }
```

```python
# services/backend/api/reports.py

# GET /api/reports
#   ?page=1&page_size=10
#   响应: PR 报道列表（含关联文章摘要）

# GET /api/reports/{report_id}
#   响应: 报道全文 Markdown + 源文章信息

# GET /api/knowledge
#   响应: 知识库加载状态 + 关键摘要
```

---

## 5. 测试策略

### 5.1 测试金字塔

```
         ╱  E2E ╲            ← POST /api/pipeline/run 全流程
        ╱────────╲
       ╱ Integration ╲       ← Tool → MCP Bridge → MCP Server
      ╱──────────────╲
     ╱   Unit Tests    ╲     ← 模型 / Prompt / 打分逻辑
    ╱──────────────────╲
```

### 5.2 测试清单

| 被测对象 | 类型 | 框架 | 关键测试点 | 预计用例数 |
|----------|------|------|------------|-----------|
| `tools.py` — MCP Tool 方法签名 | 单元 | pytest | Tool name, description, schema 正确 | 5+ |
| `tools.py` — Tool 调用（mock HTTP） | 单元 | pytest + httpx mock | 成功/超时/错误响应 | 8+ |
| `knowledge.py` — 加载逻辑 | 单元 | pytest | 缓存命中/解析/输出格式 | 5+ |
| `scorer.py` — Prompt 构建 | 单元 | pytest | System/User prompt 包含必要信息 | 4+ |
| `scorer.py` — 响应解析 | 单元 | pytest | 正常 JSON / 格式异常 / 缺字段 | 6+ |
| `scorer.py` — 端到端（mock LLM） | 单元 | pytest + mock | 输入文章 → 输出正确分数结构 | 5+ |
| `reporter.py` — Prompt 构建 | 单元 | pytest | PR 模板字段完整 | 4+ |
| `reporter.py` — 响应解析 | 单元 | pytest | Markdown 格式有效 | 3+ |
| `pipeline.py` — 状态转换 | 单元 | pytest | crawl→classify→score→report 流程 | 5+ |
| `pipeline.py` — 错误恢复 | 单元 | pytest | 某阶段失败不影响后续独立执行 | 4+ |
| API 端点 | 集成 | pytest + httpx | 各端点返回 200 + 正确结构 | 10+ |
| MCP Tool 集成 | 集成 | pytest | Tool 通过 HTTP 调用 MCP Bridge | 3+ |

### 5.3 Mock 策略

```python
# tests/conftest.py 新增 fixture

@pytest.fixture
def mock_llm():
    """Mock DeepSeek LLM 调用，返回预定义打分/报道结果"""
    with patch("langchain_openai.ChatOpenAI.ainvoke") as mock:
        mock.return_value = AIMessage(content=json.dumps({
            "ai_relevance_score": 85,
            "reportability_score": 72,
            "score_reason": "测试打分理由",
            "tags": ["MCP", "认证"],
        }))
        yield mock

@pytest.fixture
def mock_httpx_client():
    """Mock HTTP client，模拟 MCP Bridge 响应"""
    with patch("httpx.AsyncClient") as mock:
        # 配置各端点返回值
        yield mock
```

### 5.4 覆盖率目标

| 指标 | 阶段二目标 |
|------|-----------|
| 行覆盖率 | ≥ 80% |
| `agent/` 模块覆盖率 | ≥ 85% |
| `api/` 模块覆盖率 | ≥ 75% |

---

## 6. 任务精细化拆解

### 6.1 任务总览

```
任务 2.1: MCP 工具包装（LangChain Tools）
任务 2.2: 产品知识库加载器
任务 2.3: 双维度打分 Agent
任务 2.4: PR 报道生成 Agent
任务 2.5: LangGraph 流水线编排
任务 2.6: REST API 实现（Pipeline + Dashboard + Reports）
任务 2.7: 集成测试与验证
```

### 6.2 任务 2.1：MCP 工具包装（LangChain Tools）

**预计工时**: 2.5h
**分支**: `feat/2.1-mcp-tools`

| 子任务 | 说明 | 产出 |
|--------|------|------|
| 2.1.1 | 创建 `agent/tools.py`，定义工具接口 | 基类和类型定义 |
| 2.1.2 | 实现 `create_mcp_tool()` — 将 MCP HTTP API 包装为 LangChain `@tool` | 通用包装函数 |
| 2.1.3 | 注册 mcp-wewe 的 3 个 RSS 工具（fetch_yesterday / fetch_fulltext / analyze_article） | wewe_tools |
| 2.1.4 | 注册 mcp-crawl 的 5 个爬虫工具（crawl / classify / query / stats / export） | crawl_tools |
| 2.1.5 | 工具级错误处理（超时重试、HTTP 错误 → ToolException） | 错误处理 |
| 2.1.6 | 单元测试（Tool Schema 校验 + mock HTTP） | test_tools.py |

#### tools.py 接口设计

```python
# services/backend/agent/tools.py

from langchain_core.tools import tool
import httpx

# ── 通用包装 ──

def create_mcp_tool(
    name: str,
    description: str,
    endpoint: str,
    method: str = "POST",
    timeout: float = 30.0,
) -> callable:
    """将 MCP HTTP Bridge 端点包装为 LangChain Tool"""
    ...

# ── mcp-wewe 工具 ──

@tool
async def fetch_wewe_articles(rss_url: str = "") -> list[dict]:
    """获取微信公众号昨日文章列表。"""
    ...

@tool
async def fetch_article_fulltext(link: str) -> str:
    """抓取单篇微信公众号文章全文 Markdown。"""
    ...

@tool
async def analyze_wewe_article(link: str, title: str = "") -> dict:
    """AI 分析单篇公众号文章并生成摘要。"""
    ...

# ── mcp-crawl 工具 ──

@tool
async def crawl_overseas_news(days: int = 1) -> list[dict]:
    """爬取海外安全新闻。"""
    ...

@tool
async def classify_articles(articles_json: str) -> list[dict]:
    """AI 分类文章（AI安全/Agent安全）。"""
    ...

@tool
async def query_articles(category: str = "", days: int = 7, keyword: str = "") -> list[dict]:
    """查询已爬取文章。"""
    ...

@tool
async def get_crawl_stats() -> dict:
    """获取爬取统计。"""
    ...

@tool
async def export_csv(category: str = "") -> str:
    """导出 AI 安全文章 CSV。"""
    ...
```

### 6.3 任务 2.2：产品知识库加载器

**预计工时**: 2h
**分支**: `feat/2.2-knowledge-loader`

| 子任务 | 说明 | 产出 |
|--------|------|------|
| 2.2.1 | 实现 `KnowledgeLoader` 类，异步读取 docs/ 目录下的产品文档 | `knowledge.py` |
| 2.2.2 | 使用 LLM 提取结构化产品信息（定位/功能/壁垒/控标点） | 提取 Prompt |
| 2.2.3 | 内存缓存 + 热加载（检测文件变更时自动刷新） | 缓存机制 |
| 2.2.4 | `as_system_prompt()` — 将知识库转为打分/报道 Agent 的 Prompt 前缀 | Prompt 模板 |
| 2.2.5 | 单元测试（缓存命中 / 解析正确性 / Prompt 格式） | test_knowledge.py |

### 6.4 任务 2.3：双维度打分 Agent

**预计工时**: 3h
**分支**: `feat/2.3-scorer-agent`

| 子任务 | 说明 | 产出 |
|--------|------|------|
| 2.3.1 | 实现 `ScoringAgent` 类 | `scorer.py` |
| 2.3.2 | 构建 System Prompt（产品背景 + 打分维度 + 输出格式） | Prompt 模板 |
| 2.3.3 | 构建 User Prompt（文章信息 + 分类结果） | Prompt 模板 |
| 2.3.4 | 调用 DeepSeek LLM 打分，解析 JSON 响应 | LLM 调用 + 解析 |
| 2.3.5 | 并发打分控制（Semaphore + 批量提交） | 并发逻辑 |
| 2.3.6 | 分数验证（范围检查 / 必填字段 / 异常处理） | 校验逻辑 |
| 2.3.7 | 单元测试（Prompt 构建 / mock LLM 响应 / 解析） | test_scorer.py |

#### scorer.py 接口设计

```python
# services/backend/agent/scorer.py

class ScoringAgent:
    """双维度打分 Agent"""

    def __init__(self, llm: ChatDeepSeek, knowledge: ProductKnowledge):
        self.llm = llm
        self.knowledge = knowledge
        self.system_prompt = self._build_system_prompt()
        self.semaphore = asyncio.Semaphore(3)  # 并发限制

    def _build_system_prompt(self) -> str:
        """构建 System Prompt（知识库 + 打分维度）"""
        ...

    async def score_single(self, article: ArticleInDB) -> dict:
        """对单篇文章打分"""
        ...

    async def score_batch(
        self, articles: list[ArticleInDB]
    ) -> list[dict]:
        """批量并发打分"""
        ...
```

### 6.5 任务 2.4：PR 报道生成 Agent

**预计工时**: 2.5h
**分支**: `feat/2.4-reporter-agent`

| 子任务 | 说明 | 产出 |
|--------|------|------|
| 2.4.1 | 实现 `ReportAgent` 类 | `reporter.py` |
| 2.4.2 | 构建 PR 报道 System Prompt（模板 + 产品背景） | Prompt 模板 |
| 2.4.3 | 构建 User Prompt（文章全文 + 打分详情） | Prompt 模板 |
| 2.4.4 | 调用 LLM 生成报道，存入 MongoDB reports collection | 写入逻辑 |
| 2.4.5 | 更新 articles collection（has_report / report_id） | 更新逻辑 |
| 2.4.6 | 单元测试（Prompt / mock LLM / 数据库写入） | test_reporter.py |

#### reporter.py 接口设计

```python
# services/backend/agent/reporter.py

class ReportAgent:
    """PR 报道生成 Agent"""

    def __init__(self, llm: ChatDeepSeek, knowledge: ProductKnowledge, db):
        self.llm = llm
        self.knowledge = knowledge
        self.db = db
        self.system_prompt = self._build_system_prompt()

    async def generate_report(self, article: ArticleInDB) -> ReportInDB:
        """为单篇高分文章生成 PR 报道"""
        ...
```

### 6.6 任务 2.5：LangGraph 流水线编排

**预计工时**: 3.5h
**分支**: `feat/2.5-pipeline-orchestration`

| 子任务 | 说明 | 产出 |
|--------|------|------|
| 2.5.1 | 定义 `PipelineState` TypedDict（全局状态） | 状态定义 |
| 2.5.2 | 实现 `crawl_node` — 爬取阶段 | 爬取节点 |
| 2.5.3 | 实现 `classify_node` — 分类阶段 | 分类节点 |
| 2.5.4 | 实现 `score_node` — 打分阶段 | 打分节点 |
| 2.5.5 | 实现 `report_node` — 报道生成阶段 | 报道节点 |
| 2.5.6 | 组装 StateGraph + 编译 | 图构建 |
| 2.5.7 | 实现 `PipelineManager` — 管理流水线生命周期（启动/状态/停止） | 管理器 |
| 2.5.8 | 单元测试（状态转换 / 阶段间数据传递 / 错误恢复） | test_pipeline.py |

### 6.7 任务 2.6：REST API 实现

**预计工时**: 2h
**分支**: `feat/2.6-rest-api`

| 子任务 | 说明 | 产出 |
|--------|------|------|
| 2.6.1 | `api/pipeline.py` — 流水线触发 + 状态 API | 4 个端点 |
| 2.6.2 | `api/dashboard.py` — 文章列表 + 统计 API | 3 个端点 |
| 2.6.3 | `api/reports.py` — PR 报道列表 + 详情 + 知识库 API | 3 个端点 |
| 2.6.4 | 注册路由到 main.py | 路由注册 |
| 2.6.5 | API 集成测试 | test_api.py |

### 6.8 任务 2.7：集成测试与验证

**预计工时**: 1.5h
**分支**: `feat/2.7-integration-test`

| 子任务 | 说明 | 产出 |
|--------|------|------|
| 2.7.1 | Mock LLM 全流程测试 | test_pipeline_integration.py |
| 2.7.2 | MCP Tool → HTTP Bridge 集成测试 | test_mcp_integration.py |
| 2.7.3 | 端到端测试（crawl → classify → score → report） | test_e2e_pipeline.py |
| 2.7.4 | 错误场景测试（MCP 不可用 / LLM 超时 / DB 断开） | 失败场景覆盖 |

---

## 7. Day-by-Day 执行计划

### Day 3（8 工时）

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-09:30 | 站会：阶段二目标对齐 + 依赖确认 | — |
| 09:30-11:30 | **2.1** MCP 工具包装（tools.py + 单元测试） | 8 个 LangChain Tool 可用 |
| 11:30-12:00 | **2.2 前半** 知识库加载器基础实现 | KnowledgeLoader 可读取文档 |
| 12:00-13:00 | 午休 | — |
| 13:00-14:00 | **2.2 后半** LLM 提取 + 缓存 + Prompt 模板 | `as_system_prompt()` 就绪 |
| 14:00-17:00 | **2.3** 双维度打分 Agent（Prompt + LLM调用 + 解析 + 并发） | ScoringAgent 可独立打分 |
| 17:00-18:00 | **2.3 续** 打分 Agent 单元测试 + mock LLM 验证 | test_scorer.py 通过 |

**Day 3 检查点**: `agent/tools.py` + `agent/knowledge.py` + `agent/scorer.py` 就绪，相关单元测试通过。

### Day 4（8 工时）

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-09:15 | 站会：Day 3 回顾 | — |
| 09:15-11:15 | **2.4** PR 报道生成 Agent（Prompt + 生成 + 入库） | ReportAgent 可生成报道 |
| 11:15-12:00 | **2.5 前半** LangGraph 状态图定义 + crawl/classify 节点 | 前两个节点联通 |
| 12:00-13:00 | 午休 | — |
| 13:00-15:00 | **2.5 后半** score/report 节点 + PipelineManager | 完整流水线编排完成 |
| 15:00-16:30 | **2.6** REST API 实现（Pipeline + Dashboard + Reports） | 10 个 API 端点 |
| 16:30-17:30 | **2.7** 集成测试 + mock LLM 端到端验证 | 全链路测试通过 |
| 17:30-18:00 | 阶段二回顾 + 代码审查 | — |

**Day 4 检查点**: `POST /api/pipeline/run` 全流程可执行，mock 模式下端到端跑通。

---

## 8. 验收标准

### 8.1 功能验收

- [ ] `agent/tools.py` — 8 个 LangChain Tool 可独立调用，返回结构化结果
- [ ] `agent/knowledge.py` — `KnowledgeLoader.load()` 返回含产品定位/功能/壁垒的结构化数据
- [ ] `agent/scorer.py` — `ScoringAgent.score_batch(articles)` 返回 `[{ai_relevance_score, reportability_score, score_reason, tags}]`
- [ ] `agent/reporter.py` — `ReportAgent.generate_report(article)` 返回结构化 Markdown 报道
- [ ] `agent/pipeline.py` — `POST /api/pipeline/run` 触发全流程，MongoDB 中 articles 和 reports 数据完整
- [ ] `GET /api/articles` — 分页 + 筛选 + 排序 全部生效
- [ ] `GET /api/stats` — 返回 6 维统计指标
- [ ] `GET /api/reports` — PR 报道列表正确返回
- [ ] `GET /api/pipeline/status` — 返回当前运行状态
- [ ] `POST /api/pipeline/crawl` — 可单独触发爬取阶段

### 8.2 技术验收

- [ ] LangGraph 状态图正常运行（无死锁 / 无状态泄漏）
- [ ] 并发打分 asyncio.Semaphore（3 并发）生效
- [ ] LLM JSON 响应解析失败有降级处理（retry 1 次 + 记录错误）
- [ ] MCP Bridge 超时（30s）不阻塞整体流水线
- [ ] MongoDB 连接异常时流水线能优雅降级
- [ ] 知识库文件更新后 `KnowledgeLoader` 能检测并重载

### 8.3 测试验收

- [ ] `pytest tests/unit/ -k "agent"` 通过率 100%
- [ ] `agent/` 模块行覆盖率 ≥ 85%
- [ ] Mock LLM 模式下端到端测试通过
- [ ] 异常场景：MCP 不可用 / LLM 超时 / DB 断连 — 均有测试覆盖

### 8.4 文档验收

- [ ] 每个 Agent 类有 docstring（含使用示例）
- [ ] `agent/pipeline.py` 含流程图注释
- [ ] `api/pipeline.py` 每个端点有 Swagger 描述

---

## 附录

### A. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| DeepSeek API 限流 | 中 | 高 | Semaphore(3) + 指数退避重试 + 分段批量 |
| LangGraph 版本兼容性 | 低 | 中 | 固定 `langgraph>=0.2.0`，避免 latest |
| MCP Bridge 超时 | 中 | 中 | httpx timeout=30s + retry 1 次 |
| 知识库文档未维护 | 中 | 低 | 硬编码 fallback 摘要，不阻塞流水线 |
| 内存中缓存的全局状态 | 低 | 低 | PipelineManager 单例 + 状态持久化到文件 |

### B. 技术债务登记（明确不做）

1. ~~实时 WebSocket 推送流水线进度~~ → 阶段三（轮询 GET /api/pipeline/status 可替代）
2. ~~前端流水线触发面板~~ → 阶段三（先用 Swagger / curl 测试）
3. ~~评分模型微调 / A/B Test~~ → 后续阶段
4. ~~分布式任务队列（Celery / Redis）~~ → 生产化阶段
5. ~~增量爬取（去重优化 / 断点续传）~~ → 阶段四

---

> **下一阶段**: [阶段三：API 与可视化](../技术方案与实施计划.md#第三阶段api与可视化day-5-6)
>
> 🤖 本文档由人工规划，AI 辅助生成结构化内容。
