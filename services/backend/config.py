"""
后端配置管理 — 从环境变量加载，pydantic-settings 自动校验。

使用方式:
    from config import get_settings
    settings = get_settings()
    print(settings.MONGODB_URI)
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
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

    # ── 知识库 ───────────────────────────────────────
    KNOWLEDGE_BASE_DIR: str = Field(
        default="/app/docs",
        description="产品知识库文档目录",
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


# ═══════════════════════════════════════════════════════════
# 单例（避免反复解析环境变量）
# ═══════════════════════════════════════════════════════════


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（缓存，避免重复加载 .env）"""
    return Settings()
