"""
后端配置管理 — 从环境变量加载，pydantic-settings 自动校验。

使用方式:
    from config import get_settings
    settings = get_settings()
    print(settings.MONGODB_URI)
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
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

    # ── MCP 服务地址 ─────────────────────────────────
    MCP_WEWE_URL: str = Field(
        default="http://mcp-wewe:8100",
        description="mcp-wewe 服务地址",
    )
    MCP_CRAWL_URL: str = Field(
        default="http://mcp-crawl:8101",
        description="mcp-crawl 服务地址",
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


# ═══════════════════════════════════════════════════════════
# 单例（避免反复解析环境变量）
# ═══════════════════════════════════════════════════════════


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（缓存，避免重复加载 .env）"""
    return Settings()
