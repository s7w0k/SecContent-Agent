"""
后端配置管理 — 从环境变量加载，pydantic-settings 自动校验。

使用方式:
    from config import get_settings
    settings = get_settings()
    print(settings.MONGODB_URI)
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """后端全局配置，所有值从环境变量 / .env 文件加载。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── MongoDB ──────────────────────────────────────
    MONGODB_URI: str = Field(
        default="mongodb://admin:pr_agent_2024@mongodb:27017",
        description="MongoDB 连接 URI",
    )
    MONGODB_DB: str = Field(
        default="pr_agent",
        description="数据库名",
    )
    MONGODB_MAX_POOL_SIZE: int = Field(
        default=20,
        ge=2,
        le=100,
        description="连接池最大连接数",
    )
    MONGODB_MIN_POOL_SIZE: int = Field(
        default=2,
        ge=1,
        le=20,
        description="连接池最小连接数",
    )

    # ── Redis / ARQ ──────────────────────────────────
    REDIS_HOST: str = Field(default="redis", min_length=1, description="Redis host")
    REDIS_PORT: int = Field(default=6379, ge=1, le=65535, description="Redis port")
    REDIS_DB: int = Field(default=1, ge=0, le=15, description="Redis database number")
    REDIS_PASSWORD: str = Field(
        default="",
        description="Optional Redis password",
        repr=False,
    )
    ARQ_MAX_JOBS: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Maximum concurrent jobs per worker",
    )
    ARQ_JOB_TIMEOUT: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="Job timeout in seconds",
    )
    ARQ_MAX_RETRIES: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum job retry count",
    )

    # ── 全链路执行日志 ──────────────────────────────
    EXECUTION_LOG_LEVEL: str = Field(
        default="INFO",
        pattern=r"^(DEBUG|INFO|WARNING|ERROR)$",
        description="执行日志最低记录级别",
    )
    EXECUTION_LOG_RUN_RETENTION_DAYS: int = Field(default=90, ge=1, le=3650)
    EXECUTION_LOG_EVENT_RETENTION_DAYS: int = Field(default=30, ge=1, le=3650)
    EXECUTION_LOG_ERROR_RETENTION_DAYS: int = Field(default=90, ge=1, le=3650)
    EXECUTION_LOG_DEBUG_RETENTION_DAYS: int = Field(default=7, ge=1, le=3650)
    EXECUTION_LOG_QUEUE_SIZE: int = Field(default=10000, ge=100, le=1000000)
    EXECUTION_LOG_BATCH_SIZE: int = Field(default=50, ge=1, le=1000)
    EXECUTION_LOG_FLUSH_INTERVAL_MS: int = Field(default=500, ge=50, le=60000)

    # ── MCP 服务地址 ─────────────────────────────────
    MCP_WEWE_URL: str = Field(
        default="http://mcp-wewe:8100",
        description="mcp-wewe 服务地址",
    )
    MCP_CRAWL_URL: str = Field(
        default="http://mcp-crawl:8101",
        description="mcp-crawl 服务地址",
    )
    MCP_CRAWL_API_KEY: SecretStr = Field(
        default=SecretStr(""),
        description="mcp-crawl 服务间认证 Token",
    )
    MCP_CRAWL_CONNECT_TIMEOUT: float = Field(
        default=5.0,
        gt=0,
        le=60,
        description="mcp-crawl TCP 建连超时秒数",
    )
    MCP_CRAWL_READ_TIMEOUT: float = Field(
        default=300.0,
        gt=0,
        le=1800,
        description="mcp-crawl 响应读取超时秒数",
    )
    MCP_CRAWL_MAX_RETRIES: int = Field(
        default=2,
        ge=0,
        le=5,
        description="mcp-crawl 可重试错误的最大重试次数",
    )
    MCP_CRAWL_MAX_RESPONSE_MB: int = Field(
        default=20,
        ge=1,
        le=100,
        description="mcp-crawl 最大响应体积（MiB）",
    )
    MCP_CRAWL_VERIFY_TLS: bool = Field(
        default=True,
        description="是否校验 mcp-crawl HTTPS 证书",
    )

    # ── SearXNG 搜索 ─────────────────────────────────
    WEB_SEARCH_ENABLED: bool = Field(default=False, description="搜索功能总开关")
    SEARXNG_URL: str = Field(default="http://searxng:8080", description="SearXNG 内部地址")
    SEARXNG_CONNECT_TIMEOUT: float = Field(
        default=3.0, gt=0, le=30, description="SearXNG 建连超时秒数"
    )
    SEARXNG_READ_TIMEOUT: float = Field(
        default=15.0, gt=0, le=60, description="SearXNG 读取超时秒数"
    )
    SEARXNG_MAX_RETRIES: int = Field(
        default=1, ge=0, le=3, description="SearXNG 最大重试次数"
    )
    WEB_SEARCH_RESULT_LIMIT: int = Field(
        default=20, ge=1, le=50, description="单页返回结果上限"
    )
    WEB_SEARCH_IMPORT_BATCH_LIMIT: int = Field(
        default=20, ge=1, le=50, description="单批导入上限"
    )
    WEB_SEARCH_SESSION_TTL_MINUTES: int = Field(
        default=30, ge=5, le=120, description="搜索会话保留时间分钟"
    )
    WEB_SEARCH_RATE_LIMIT_PER_MINUTE: int = Field(
        default=20, ge=1, le=100, description="单用户搜索频率"
    )
    WEB_SEARCH_CACHE_TTL_MINUTES: int = Field(
        default=10, ge=0, le=60, description="搜索结果缓存分钟数，0 表示禁用"
    )
    WEB_SEARCH_ALLOWED_CATEGORIES: str = Field(
        default="general,news", description="允许的搜索分类"
    )
    WEB_SEARCH_ALLOWED_LANGUAGES: str = Field(
        default="all,zh-CN,en", description="允许的搜索语言"
    )
    WEB_SEARCH_ENRICH_ON_IMPORT: bool = Field(
        default=True, description="导入后是否自动补全文"
    )
    WEB_SEARCH_FETCH_MAX_CONCURRENCY: int = Field(
        default=3, ge=1, le=10, description="全文抓取最大并发"
    )
    WEB_SEARCH_AUDIT_RETENTION_DAYS: int = Field(
        default=180, ge=1, le=3650, description="导入审计保留天数"
    )

    # ── 阶段十四 Feature Flags ──────────────────────────
    USER_PROMPT_V2_ENABLED: bool = Field(
        default=True, description="启用用户级提示词中心（T1-T2）"
    )
    PRODUCT_CATALOG_ENABLED: bool = Field(
        default=True, description="启用产品目录和知识切片（T3）"
    )
    GENERATION_PREFERENCES_ENABLED: bool = Field(
        default=True, description="启用用户级生成偏好（T4）"
    )
    USER_ASSESSMENT_ENABLED: bool = Field(
        default=True, description="启用用户级文章评估（T5）"
    )
    PIPELINE_CONFIG_FREEZE_ENABLED: bool = Field(
        default=True, description="启用流水线配置冻结（T6）"
    )
    LEGACY_GLOBAL_SCORE_AS_FALLBACK: bool = Field(
        default=True, description="旧全局分数作为回退（关闭后仅展示用户级分数）"
    )
    USER_KNOWLEDGE_ENABLED: bool = Field(
        default=True, description="启用用户级产品知识库（阶段十五）"
    )

    # ── 海外新闻每日定时抓取（阶段十六） ──────────────
    OVERSEAS_NEWS_SCHEDULE_ENABLED: bool = Field(
        default=True, description="是否启用每日定时抓取"
    )
    OVERSEAS_NEWS_SCHEDULE_TIMEZONE: str = Field(
        default="Asia/Shanghai", description="业务时区（IANA 时区名）"
    )
    OVERSEAS_NEWS_SCHEDULE_HOUR: int = Field(
        default=7, ge=0, le=23, description="当地小时（0-23）"
    )
    OVERSEAS_NEWS_SCHEDULE_MINUTE: int = Field(
        default=0, ge=0, le=59, description="当地分钟（0-59）"
    )
    OVERSEAS_NEWS_SCHEDULE_CRAWL_DAYS: int = Field(
        default=1, ge=1, le=7, description="抓取最近天数（1-7）"
    )
    OVERSEAS_NEWS_JOB_TIMEOUT_SECONDS: int = Field(
        default=1200, ge=300, le=3600, description="元数据任务超时（秒）"
    )
    OVERSEAS_NEWS_LOCK_TTL_SECONDS: int = Field(
        default=1500, ge=600, le=7200, description="共享锁 TTL（秒，须大于任务超时）"
    )
    OVERSEAS_NEWS_RUN_RETENTION_DAYS: int = Field(
        default=90, ge=7, le=365, description="执行记录保留天数"
    )
    OVERSEAS_NEWS_STARTUP_CATCHUP_ENABLED: bool = Field(
        default=True, description="是否启用当日漏跑补偿"
    )

    # ── Agent Loop（阶段一）──────────────────────────────
    CHAT_AGENT_ENABLED: bool = Field(
        default=False, description="Agent Loop 总开关（默认关闭，灰度时开启）"
    )
    CHAT_ASK_AGENT_ENABLED: bool = Field(
        default=False, description="问答 Agent 子开关（总开关开启后仍需单独开启）"
    )
    CHAT_REVISE_AGENT_ENABLED: bool = Field(
        default=False, description="改稿 Agent 子开关（ask 稳定后再开启）"
    )
    CHAT_AGENT_SHADOW_ENABLED: bool = Field(
        default=False, description="影子模式：后台执行 Agent 但不返回给用户"
    )
    CHAT_AGENT_ROLLOUT_PERCENT: int = Field(
        default=0, ge=0, le=100, description="灰度百分比（0-100）"
    )
    CHAT_AGENT_MAX_ROUNDS: int = Field(
        default=5, ge=1, le=20, description="Agent Loop 最大轮次"
    )
    CHAT_AGENT_MAX_INPUT_TOKENS: int = Field(
        default=24000, ge=1000, le=128000, description="Agent Loop 输入 token 上限"
    )
    CHAT_AGENT_MAX_OUTPUT_TOKENS: int = Field(
        default=4000, ge=100, le=32000, description="Agent Loop 输出 token 上限"
    )
    CHAT_AGENT_MAX_TOOL_CALLS: int = Field(
        default=8, ge=1, le=50, description="Agent Loop 最大工具调用次数"
    )
    CHAT_AGENT_MAX_PARALLEL_TOOLS: int = Field(
        default=3, ge=1, le=10, description="同轮最大并行工具数"
    )
    CHAT_AGENT_DEADLINE_SECONDS: int = Field(
        default=30, ge=5, le=120, description="Agent Loop 运行超时秒数"
    )
    CHAT_AGENT_TOOL_TIMEOUT_SECONDS: int = Field(
        default=5, ge=1, le=30, description="单个工具执行超时秒数"
    )
    CHAT_AGENT_EVENT_TTL_DAYS: int = Field(
        default=30, ge=1, le=365, description="Agent 事件保留天数"
    )
    CHAT_AGENT_MAX_COST_USD: float = Field(
        default=0.0, ge=0, description="单次 Agent 运行成本上限（USD），0=不限制"
    )
    CHAT_SSE_SCHEMA_VERSION: str = Field(
        default="1.0", description="SSE 事件 schema 版本"
    )
    CHAT_AGENT_MEMORY_ENABLED: bool = Field(
        default=True, description="聊天 Agent 注入用户长期记忆（token 预算内）"
    )
    CHAT_AGENT_SKILL_ENABLED: bool = Field(
        default=True, description="聊天 Agent 按诉求召回并注入 Skill 指令"
    )
    CHAT_AGENT_HISTORY_TOKENS: int = Field(
        default=6000, ge=1000, le=64000, description="聊天 Agent 单轮历史 token 预算"
    )
    CHAT_AGENT_EVOLUTION_ENABLED: bool = Field(
        default=True, description="聊天 Agent 自进化闭环（落库 generation_runs + 记忆事件）"
    )

    # ── 知识 Skills 与上下文工程（阶段二）──────────────────────
    KNOWLEDGE_SKILLS_ENABLED: bool = Field(
        default=False, description="知识 Skills/ContextManager 总开关（默认关闭）"
    )
    KNOWLEDGE_SKILLS_SHADOW_ENABLED: bool = Field(
        default=False,
        description="影子模式：后台构建 ContextPlan 仅记录差异，LLM 仍用旧上下文",
    )
    KNOWLEDGE_SKILLS_ROLLOUT_PERCENT: int = Field(
        default=0, ge=0, le=100, description="灰度百分比（0-100，按 user_id 确定性分流）"
    )
    CONTEXT_MAX_INPUT_TOKENS: int = Field(
        default=0, ge=0, description="输入 token 上限；0=按模型窗口动态推导"
    )
    CONTEXT_CACHE_TTL_SECONDS: int = Field(
        default=300, ge=1, le=86400, description="上下文缓存 TTL（秒，兜底）"
    )
    CONTEXT_OFFLINE_COMPRESSION_ENABLED: bool = Field(
        default=False, description="离线上下文压缩开关（默认关闭）"
    )

    # ── MultiAgent 编排（阶段三）────────────────────────────────
    MULTI_AGENT_ENABLED: bool = Field(
        default=False, description="MultiAgent 编排总开关（默认关闭，灰度时开启）"
    )
    MULTI_AGENT_SHADOW_ENABLED: bool = Field(
        default=False,
        description="影子模式：后台执行 planned 流水线仅记录差异，不回填业务产物",
    )
    MULTI_AGENT_ROLLOUT_PERCENT: int = Field(
        default=0, ge=0, le=100, description="灰度百分比（0-100，按 user_id 确定性分流）"
    )
    PLANNER_MODEL: str = Field(
        default="", description="Planner 小模型名；空=禁用 LLM 规划，直接默认 DAG"
    )
    PLANNER_TIMEOUT_SECONDS: int = Field(
        default=10, ge=1, le=120, description="Planner LLM 单次调用超时（秒）"
    )
    PLAN_MAX_STEPS: int = Field(
        default=50, ge=1, le=100, description="计划最大步骤数"
    )
    PLAN_MAX_DEPTH: int = Field(
        default=10, ge=1, le=20, description="计划最大依赖深度"
    )
    ORCHESTRATOR_MAX_CONCURRENCY: int = Field(
        default=5, ge=1, le=50, description="Orchestrator 全局最大并发 Worker"
    )
    ORCHESTRATOR_USER_CONCURRENCY: int = Field(
        default=2, ge=1, le=10, description="单用户最大并发 Worker"
    )
    WORKER_LEASE_SECONDS: int = Field(
        default=120, ge=10, le=3600, description="Worker 步骤租约 TTL（秒）"
    )
    WORKER_MAX_ATTEMPTS: int = Field(
        default=3, ge=1, le=10, description="Worker 最大尝试次数"
    )

    # ── 全自主 Agent（阶段四 4A，默认关闭）────────────────────────
    AUTONOMOUS_AGENT_ENABLED: bool = Field(
        default=False, description="自主模式总开关（默认关闭；开启后仍独立于标准/AgentLoop）"
    )
    AUTONOMOUS_AGENT_SHADOW_ENABLED: bool = Field(
        default=False, description="影子运行：后台执行自主流程仅记录差异，不返回用户"
    )
    AUTONOMOUS_AGENT_ROLLOUT_PERCENT: int = Field(
        default=0, ge=0, le=100, description="灰度百分比（0-100，按 user_id 确定性分流）"
    )
    AGENT_PLANNER: str = Field(
        default="rule",
        description="自主运行选步模式：rule=固定顺序 SOP；llm=LLM 每轮决策下一步（关卡护栏不变，LLM 故障自动回退 rule）",
    )
    AUTONOMOUS_MAX_STEPS: int = Field(
        default=20, ge=1, le=100, description="自主运行最大步骤数"
    )
    AUTONOMOUS_MAX_RUNTIME_SECONDS: int = Field(
        default=600, ge=30, le=86400, description="自主运行最大墙钟时间（秒）"
    )
    AUTONOMOUS_MAX_INPUT_TOKENS: int = Field(
        default=24000, ge=1000, le=256000, description="自主运行输入 token 上限"
    )
    AUTONOMOUS_MAX_OUTPUT_TOKENS: int = Field(
        default=4000, ge=100, le=64000, description="自主运行输出 token 上限"
    )
    AUTONOMOUS_MAX_TOTAL_TOKENS: int = Field(
        default=0, ge=0, le=320000, description="输入+输出总 token 上限；0=由单项上限兜底"
    )
    AUTONOMOUS_MAX_TOOL_CALLS: int = Field(
        default=40, ge=1, le=200, description="自主运行最大工具调用次数"
    )
    AUTONOMOUS_MAX_PARALLEL_TOOLS: int = Field(
        default=3, ge=1, le=10, description="同轮最大并行工具数"
    )
    AUTONOMOUS_MAX_TOOL_CONCURRENCY: int = Field(
        default=3, ge=1, le=20, description="自主工具最大并发数"
    )
    AUTONOMOUS_MAX_COST_USD: float = Field(
        default=0.0, ge=0, description="单次自主运行成本上限（USD），0=不限制"
    )
    AUTONOMOUS_MAX_RETRIES: int = Field(
        default=2, ge=0, le=10, description="单步最大重试次数（不含首次）"
    )
    AUTONOMOUS_MAX_CONSECUTIVE_FAILURES: int = Field(
        default=3, ge=1, le=20, description="连续失败阈值，达到即终止"
    )
    AUTONOMOUS_LOOP_SIMILARITY_THRESHOLD: float = Field(
        default=0.90, ge=0, le=1, description="最近 N 步计划/动作相似度阈值，超出判定为循环"
    )
    AUTONOMOUS_APPROVAL_TTL_SECONDS: int = Field(
        default=1800, ge=60, le=86400, description="人工审批授权有效期（秒）"
    )
    AUTONOMOUS_EVENT_TTL_DAYS: int = Field(
        default=30, ge=1, le=365, description="runtime_events 保留天数"
    )
    AUTONOMOUS_MODEL: str = Field(
        default="deepseek-chat", description="自主模式默认模型名"
    )
    AUTONOMOUS_ROUTER_FALLBACK_MODEL: str = Field(
        default="", description="模型路由回退链（逗号分隔）；空=不降级"
    )
    AUTONOMOUS_SSE_SCHEMA_VERSION: str = Field(
        default="1.0", description="自主模式 SSE 事件 schema 版本"
    )
    AUTONOMOUS_CONTEXT_MAX_CHARS: int = Field(
        default=12000, ge=1000, le=100000, description="自主运行上下文最大字符数（记忆/历史压缩）"
    )

    # ── 阶段3 可控追溯与错误恢复（Durable Runtime / Outbox）────────────
    CODE_REVISION: str = Field(
        default="",
        description="部署注入的 Git commit 或镜像 digest；空则 RunManifest 回退 dev-local",
    )
    TOOL_REGISTRY_VERSION: str = Field(
        default="1.0", description="工具契约注册表版本（RunManifest 启动前冻结）"
    )
    AUTONOMOUS_LEASE_SECONDS: int = Field(
        default=120,
        ge=10,
        le=3600,
        description="Autonomous run 租约 TTL（秒；worker 心跳续期，reaper 据此回收）",
    )
    AUTONOMOUS_HEARTBEAT_SECONDS: int = Field(
        default=30, ge=5, le=600, description="run 心跳间隔（秒，须远小于租约 TTL）"
    )
    RUN_REAPER_INTERVAL_SECONDS: int = Field(
        default=60, ge=10, le=3600, description="stale running 扫描间隔（秒，不允许永久 running）"
    )
    OUTBOX_MAX_ATTEMPTS: int = Field(
        default=5, ge=1, le=20, description="outbox 投递最大尝试次数（超过进入独立 dead-letter）"
    )
    OUTBOX_BATCH_SIZE: int = Field(
        default=100, ge=1, le=1000, description="outbox 单批对账投递条数"
    )

    # ── A2A 互操作（阶段四 4B，默认关闭）────────────────────────
    A2A_ENABLED: bool = Field(
        default=False, description="A2A 1.0 协议服务总开关（默认关闭；开启需先启用自主模式）"
    )
    A2A_ALLOWED_PEERS: list[str] = Field(
        default_factory=list, description="外部 Agent 允许列表（base_url）；空=仅本机闭环"
    )
    A2A_SKILL_ID: str = Field(
        default="pr_intel", max_length=100, description="首批试点开放的只读 Skill id"
    )
    A2A_SKILL_NAME: str = Field(
        default="PR 情报分析", max_length=200, description="首批试点开放的 Skill 名称"
    )
    A2A_SKILL_DESCRIPTION: str = Field(
        default="PR 情报检索、分类、打分与导出（只读低风险，供受控试点）",
        max_length=2000,
    )
    A2A_AGENT_NAME: str = Field(
        default="PR 情报智能体", max_length=200, description="Agent Card 名称"
    )
    A2A_AGENT_DESCRIPTION: str = Field(
        default="PR 情报分析 A2A Agent（A2A 1.0，HTTP+JSON/REST）",
        max_length=4000,
    )
    A2A_AGENT_URL: str = Field(
        default="http://a2a.internal/.well-known/agent-card.json",
        max_length=2000,
        description="Agent Card 对外 URL",
    )
    A2A_PRINCIPAL_PREFIX: str = Field(
        default="a2a", max_length=32, description="A2A service principal 用户前缀（不冒充最终用户）"
    )
    # A2A Client（阶段四 4B-3/4B-4，默认关闭）
    A2A_CLIENT_ENABLED: bool = Field(
        default=False, description="A2A Client 总开关（默认关闭；外部 Agent 只能通过允许列表接入）"
    )
    A2A_CLIENT_CARD_TTL_SECONDS: int = Field(
        default=300, ge=30, le=3600, description="Agent Card 缓存 TTL（秒）"
    )
    A2A_CLIENT_TIMEOUT_SECONDS: float = Field(
        default=30.0, ge=1, le=300, description="远端调用超时（秒）"
    )
    A2A_CLIENT_RETRY_MAX: int = Field(
        default=2, ge=0, le=5, description="远端调用有限重试次数（仅超时/限流/断流）"
    )
    A2A_CLIENT_MAX_CONCURRENCY: int = Field(
        default=4, ge=1, le=32, description="每远端最大并发调用数"
    )
    A2A_CLIENT_RPS: float = Field(
        default=5.0, ge=0.1, le=1000.0, description="每远端调用速率上限（次/秒）"
    )
    A2A_CLIENT_PEER_MAX_STEPS: int = Field(
        default=20, ge=1, le=1000, description="每远端调用预算：最大步骤数"
    )
    A2A_CLIENT_PEER_QUOTA: int = Field(
        default=5, ge=1, le=1000, description="每远端调用配额（remote_agent_quota）"
    )

    # ── 阶段4 Harness/灰度/生产上线 ─────────────────────────────
    FAULT_HARNESS_ENABLED: bool = Field(
        default=False, description="故障注入 Harness 总开关（默认关闭，仅演练时开启）"
    )
    CAPACITY_SAFETY_FACTOR: float = Field(
        default=0.8, gt=0, le=1, description="容量模型安全系数（产出容量=理论值×系数）"
    )
    ROLLOUT_MIN_SAMPLE_SIZE: int = Field(
        default=200, ge=1, le=1_000_000, description="灰度档位推进的最小成功样本量"
    )
    ROLLOUT_OBSERVATION_WINDOW_SECONDS: int = Field(
        default=86_400, ge=60, le=2_592_000, description="灰度档位观察窗口（秒，不以自然日替代样本量）"
    )
    ROLLOUT_LATENCY_P95_SLO_SECONDS: float = Field(
        default=5.0, gt=0, le=300, description="端到端 p95 时延 SLO 阈值（秒）"
    )
    ROLLOUT_USD_PER_SUCCESS_BUDGET_DELTA_PCT: float = Field(
        default=10.0, gt=0, le=500, description="USD/success 超预算告警百分比阈值"
    )

    @model_validator(mode="after")
    def autonomous_enabled_requires_bounded_budget(self) -> Settings:
        """配置非法时阻止自主模式启动，而不是使用无上限默认值。"""
        if self.A2A_ENABLED and not self.AUTONOMOUS_AGENT_ENABLED:
            raise ValueError(
                "A2A_ENABLED=true 必须同时启用 AUTONOMOUS_AGENT_ENABLED；"
                "A2A 复用自主运行服务，不允许单独暴露"
            )
        if not self.AUTONOMOUS_AGENT_ENABLED:
            return self
        required_positive = {
            "AUTONOMOUS_MAX_STEPS": self.AUTONOMOUS_MAX_STEPS,
            "AUTONOMOUS_MAX_RUNTIME_SECONDS": self.AUTONOMOUS_MAX_RUNTIME_SECONDS,
            "AUTONOMOUS_MAX_INPUT_TOKENS": self.AUTONOMOUS_MAX_INPUT_TOKENS,
            "AUTONOMOUS_MAX_OUTPUT_TOKENS": self.AUTONOMOUS_MAX_OUTPUT_TOKENS,
            "AUTONOMOUS_MAX_TOOL_CALLS": self.AUTONOMOUS_MAX_TOOL_CALLS,
            "AUTONOMOUS_MAX_RETRIES": self.AUTONOMOUS_MAX_RETRIES,
            "AUTONOMOUS_MAX_CONSECUTIVE_FAILURES": self.AUTONOMOUS_MAX_CONSECUTIVE_FAILURES,
        }
        for name, value in required_positive.items():
            if value <= 0:
                raise ValueError(
                    f"AUTONOMOUS_AGENT_ENABLED=true 时 {name} 必须 > 0，当前为 {value}；"
                    "拒绝启动以避免无上限运行"
                )
        return self


    @field_validator("OVERSEAS_NEWS_SCHEDULE_TIMEZONE")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"无效的 IANA 时区: {v}") from exc
        return v

    @field_validator("OVERSEAS_NEWS_LOCK_TTL_SECONDS")
    @classmethod
    def validate_lock_ttl(cls, v: int, info) -> int:
        timeout = info.data.get("OVERSEAS_NEWS_JOB_TIMEOUT_SECONDS", 1200)
        if v <= timeout:
            raise ValueError(
                f"OVERSEAS_NEWS_LOCK_TTL_SECONDS({v}) 必须大于 "
                f"OVERSEAS_NEWS_JOB_TIMEOUT_SECONDS({timeout})"
            )
        return v

    # ── LLM 配置 ─────────────────────────────────────
    DEEPSEEK_API_KEY: str = Field(
        default="",
        description="DeepSeek API Key（必填，无默认值时应显式报错）",
    )
    DEEPSEEK_BASE_URL: str = Field(
        default="https://api.deepseek.com",
        description="DeepSeek API 基础 URL",
    )
    DEEPSEEK_MODEL: str = Field(
        default="deepseek-chat",
        description="默认模型名",
    )
    DEEPSEEK_TIMEOUT: float = Field(
        default=60.0,
        description="DeepSeek API 单次请求超时（秒）",
    )
    DEEPSEEK_MAX_TOKENS: int = Field(
        default=8192,
        description="DeepSeek API 单次请求最大生成 token 数（推理模型需留足推理+输出空间）",
    )
    DRAFT_MAX_OUTPUT_TOKENS: int = Field(
        default=1800,
        ge=256,
        le=8192,
        description="PR 草稿单次调用最大输出 token，避免沿用全局 8192 上限",
    )
    SINGLE_ARTICLE_DRAFT_VARIANTS: int = Field(
        default=1,
        ge=1,
        le=4,
        description="单篇生成默认版本数；用户可在请求中覆盖为 1~4",
    )

    # ── 知识库 ───────────────────────────────────────
    KNOWLEDGE_BASE_DIR: str = Field(
        default="/app/docs",
        description="产品知识库文档目录",
    )
    KNOWLEDGE_INDEX_PATH: str = Field(
        default="/app/docs/_index/kb-index.json",
        description="轻量知识库 JSON 索引路径",
    )
    # 检索开关（阶段四仅建立索引，不改变 active 检索行为）
    KNOWLEDGE_RETRIEVAL_ENABLED: bool = Field(
        default=False,
        description="总开关：启用内部文档检索（阶段五起启用）",
    )
    KNOWLEDGE_RETRIEVAL_SHADOW_ENABLED: bool = Field(
        default=True,
        description="影子开关：构建新上下文但 LLM 仍使用旧上下文",
    )
    KNOWLEDGE_RETRIEVAL_ROLLOUT_PERCENT: int = Field(
        default=0,
        ge=0,
        le=100,
        description="检索灰度百分比（0~100）",
    )
    KNOWLEDGE_LLM_RERANK_ENABLED: bool = Field(
        default=False,
        description="是否启用 LLM 文档重排（阶段八按评测启用）",
    )
    KNOWLEDGE_EMBEDDING_ENABLED: bool = Field(
        default=False,
        description="是否启用 embedding 召回（阶段八按评测启用）",
    )
    KNOWLEDGE_LLM_RERANK_TOP_N: int = Field(
        default=12,
        ge=1,
        le=50,
        description="LLM 文档重排只处理 BM25 Top-N 候选（阶段八 11.1）",
    )
    KNOWLEDGE_EMBEDDING_DIM: int = Field(
        default=0,
        ge=0,
        description="embedding 向量维度（0 表示按首个向量自适应）",
    )
    KNOWLEDGE_EMBEDDING_WEIGHT: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="embedding 分数在混合排序中的权重（阶段八 11.3，默认 0 即不启用）",
    )
    KNOWLEDGE_MAX_OPTIONAL_DOCS: int = Field(
        default=6,
        ge=0,
        le=20,
        description="可选文档最大召回数",
    )
    KNOWLEDGE_MAX_EXPANDED_SECTIONS: int = Field(
        default=2,
        ge=0,
        le=10,
        description="每次最多展开的章节数",
    )

    # ── LLM Wiki（SecContent-Agent LLM Wiki Knowledge Layer）─────────────
    # 知识后端：legacy=旧链路 | wiki=Wiki 为主 | shadow=旧结果同时后台跑 Wiki
    # GOAL B/§20：默认必须是 wiki；legacy 只能显式 rollback/shadow。
    KNOWLEDGE_BACKEND: str = Field(
        default="wiki",
        pattern=r"^(legacy|wiki|shadow)$",
        description="知识后端模式：wiki=Wiki 为主（默认），legacy=显式回滚，shadow=对比期",
    )
    # Wiki Root（默认位于知识库根目录下）
    WIKI_ROOT_DIR: str = Field(default="", description="Wiki 根目录；空则取 KNOWLEDGE_BASE_DIR/_wiki")
    # 各类任务单次导航最多打开的页面数
    WIKI_MAX_PAGES_SCORE: int = Field(default=6, ge=1, le=50, description="评分导航最大页面数")
    WIKI_MAX_PAGES_DRAFT: int = Field(default=8, ge=1, le=50, description="草稿导航最大页面数")
    WIKI_MAX_PAGES_CHAT: int = Field(default=8, ge=1, le=50, description="Chat 导航最大页面数")
    # 导航深度 / 输入 token 预算
    WIKI_MAX_DEPTH: int = Field(default=4, ge=1, le=8, description="Wiki 导航最大深度")
    WIKI_MAX_INPUT_TOKENS: int = Field(
        default=12000, ge=1000, le=128000, description="Wiki 单任务输入 token 预算"
    )
    # Grounding 要求
    WIKI_REQUIRE_SOURCE_GROUNDING: bool = Field(
        default=True, description="要求 Evidence 必须 grounding 到 Raw Source"
    )
    # 自动编译 / 自动发布（默认关闭；Maintainer 只写 staging，发布经 Gate）
    WIKI_AUTO_COMPILE_ENABLED: bool = Field(
        default=False, description="知识变更后是否自动编译 Wiki"
    )
    WIKI_AUTO_PUBLISH_ENABLED: bool = Field(
        default=False, description="编译通过后是否自动发布 Wiki"
    )
    # Navigator 是否允许 LLM 决策（False=仅确定性工具）
    WIKI_NAVIGATOR_LLM_ENABLED: bool = Field(
        default=True, description="Wiki Navigator 是否启用 LLM 决策"
    )
    # Grounding Verifier 进入 scoring evidence 的置信度门槛
    WIKI_EVIDENCE_CONFIDENCE_THRESHOLD: float = Field(
        default=0.8, ge=0, le=1, description="EvidenceItem 进入 scoring 的最低置信度"
    )
    # PR-A：LLM Navigator 防循环上限（exceed → 本请求禁用 LLM 决策）
    WIKI_NAVIGATOR_MAX_INVALID_ACTIONS: int = Field(
        default=2, ge=1, le=20, description="LLM 非法动作上限，超出后本请求禁用 LLM 导航"
    )
    WIKI_NAVIGATOR_MAX_LLM_FAILURES: int = Field(
        default=2, ge=1, le=20, description="LLM 决策失败上限，超出后本请求禁用 LLM 导航"
    )
    # PR-B：Evidence → Requirement 的相关性门槛
    WIKI_EVIDENCE_RELEVANCE_THRESHOLD: float = Field(
        default=0.5, ge=0, le=1, description="EvidenceItem 计入 Requirement 的最低相关性"
    )
    # PR-B：各任务类型的最小 Requirement Coverage 阈值
    WIKI_MIN_COVERAGE_SCORE: float = Field(
        default=0.70, ge=0, le=1, description="score 任务最小 Evidence Coverage"
    )
    WIKI_MIN_COVERAGE_DRAFT: float = Field(
        default=0.80, ge=0, le=1, description="draft 任务最小 Evidence Coverage"
    )
    WIKI_MIN_COVERAGE_CHAT: float = Field(
        default=0.60, ge=0, le=1, description="chat 任务最小 Evidence Coverage"
    )

    # ── 流水线 ───────────────────────────────────────
    PIPELINE_SCORE_THRESHOLD: int = Field(
        default=140,
        ge=0,
        le=200,
        description="触发 PR 报道的综合分阈值 (ai_relevance + reportability)",
    )
    PIPELINE_CRAWL_DEFAULT_DAYS: int = Field(
        default=1,
        ge=1,
        le=30,
        description="默认爬取天数",
    )

    # ── API ──────────────────────────────────────────
    API_PAGE_SIZE_MAX: int = Field(
        default=100,
        ge=10,
        le=500,
        description="分页最大每页条数",
    )

    # ── 用户记忆与个性化 ──────────────────────────────
    MEMORY_FEATURE_ENABLED: bool = Field(
        default=False,
        description="总开关：启用用户记忆学习与场景化检索",
    )
    MEMORY_DUAL_WRITE_ENABLED: bool = Field(
        default=False,
        description="双写开关：新事件同时写入 user_memory_events",
    )
    MEMORY_READ_MODE: str = Field(
        default="legacy",
        pattern=r"^(legacy|shadow|memory|fallback)$",
        description="记忆读取模式：legacy=旧画像, shadow=影子, memory=新记忆, fallback=优先新记忆",
    )
    MEMORY_AUTO_APPROVAL: bool = Field(
        default=False,
        description="自动审批：高置信度记忆自动设为 active",
    )
    MEMORY_ACTIVE_THRESHOLD: float = Field(default=0.70, ge=0, le=1)
    MEMORY_PENDING_THRESHOLD: float = Field(default=0.45, ge=0, le=1)
    MEMORY_GLOBAL_THRESHOLD: float = Field(default=0.90, ge=0, le=1)
    MEMORY_MAX_PACK_ITEMS: int = Field(default=8, ge=1, le=20)
    MEMORY_MAX_PACK_CHARS: int = Field(default=800, ge=100, le=2000)
    MEMORY_MIN_INDEPENDENT_TASKS: int = Field(default=2, ge=1, le=10)
    MEMORY_EVIDENCE_LIMIT: int = Field(default=20, ge=5, le=100)
    MEMORY_DECAY_HALF_LIFE_DAYS: int = Field(default=90, ge=1, le=365)
    PERSONALIZATION_EXPLANATION_ENABLED: bool = Field(
        default=False,
        description="前端个性化解释组件开关",
    )
    PERSONALIZATION_EXPERIMENT_ENABLED: bool = Field(
        default=False,
        description="个性化实验分流开关",
    )

    API_PAGE_SIZE_DEFAULT: int = Field(
        default=20,
        ge=1,
        le=100,
        description="分页默认每页条数",
    )
    BACKEND_PORT: int = Field(
        default=8000,
        description="后端监听端口",
    )

    # ── CORS ─────────────────────────────────────────
    CORS_ORIGINS: list[str] = Field(
        default=["http://localhost:5173", "http://localhost:8000"],
        description="允许的跨域来源",
    )

    # ── 日志文件 ───────────────────────────────────────
    LOG_DIR: str = Field(
        default="/app/logs",
        description="日志文件根目录（为空则不写文件，仅输出到控制台）",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="全局日志级别：DEBUG/INFO/WARNING/ERROR/CRITICAL",
    )
    LOG_APP_RETENTION_DAYS: int = Field(
        default=30,
        ge=1,
        le=365,
        description="应用日志保留天数",
    )
    LOG_ERROR_RETENTION_DAYS: int = Field(
        default=90,
        ge=1,
        le=730,
        description="错误日志保留天数",
    )
    LOG_ACCESS_RETENTION_DAYS: int = Field(
        default=7,
        ge=1,
        le=90,
        description="访问日志保留天数",
    )
    LOG_AUDIT_RETENTION_DAYS: int = Field(
        default=365,
        ge=1,
        le=2555,
        description="审计日志保留天数",
    )

    # ── JWT 认证 ─────────────────────────────────────
    JWT_SECRET: str = Field(
        default="",
        description="JWT 签名密钥（仅从环境变量读取，生产环境必须设置）",
        repr=False,
    )
    JWT_ALGORITHM: str = Field(
        default="HS256",
        min_length=1,
        description="JWT 签名算法",
    )
    JWT_EXPIRE_HOURS: int = Field(
        default=24,
        ge=1,
        le=720,
        description="JWT 访问令牌有效期（小时）",
    )

    # ── 校验 ─────────────────────────────────────────
    @field_validator("DEEPSEEK_API_KEY")
    @classmethod
    def deepseek_key_required(cls, v: str) -> str:
        """生产环境下 DeepSeek API Key 为必填。
        开发/测试阶段允许空值（mock），但记录警告。
        """
        if not v:
            import logging

            logging.getLogger("backend.config").warning(
                "DEEPSEEK_API_KEY is not set — LLM features will fail"
            )
        return v

    @field_validator("JWT_SECRET")
    @classmethod
    def jwt_secret_required_in_production(cls, v: str) -> str:
        """开发基线允许暂未配置；认证启用前必须通过环境变量设置。"""
        if not v:
            import logging

            logging.getLogger("backend.config").warning(
                "JWT_SECRET is not set — authentication features will fail"
            )
        return v

    @field_validator("MONGODB_URI")
    @classmethod
    def mongo_uri_format(cls, v: str) -> str:
        if not v.startswith("mongodb://") and not v.startswith("mongodb+srv://"):
            raise ValueError("Invalid MongoDB URI: must start with mongodb:// or mongodb+srv://")
        return v

    @field_validator("MCP_CRAWL_URL")
    @classmethod
    def mcp_crawl_url_format(cls, v: str) -> str:
        """只允许 HTTP(S) Bridge 地址，并统一去掉末尾斜杠。"""
        normalized = v.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("MCP_CRAWL_URL must start with http:// or https://")
        return normalized

    @field_validator("SEARXNG_URL")
    @classmethod
    def searxng_url_format(cls, v: str) -> str:
        """只允许 HTTP(S) SearXNG 地址，并统一去掉末尾斜杠。"""
        normalized = v.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("SEARXNG_URL must start with http:// or https://")
        return normalized


# ═══════════════════════════════════════════════════════════
# 单例（避免反复解析环境变量）
# ═══════════════════════════════════════════════════════════


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（缓存，避免重复加载 .env）"""
    return Settings()
