"""
Article MongoDB 数据模型 — 安全新闻文章。

字段设计:
  - url_hash: URL MD5 唯一键，用于去重
  - source_type: 来源类型（海外新闻 / 公众号 / 论文）
  - 双维度评分: ai_relevance_score + reportability_score
  - 关联: has_report → report_id 指向 PR 报道
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

# ═══════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════


class SourceType(StrEnum):
    """文章来源类型"""

    OVERSEAS_NEWS = "overseas_news"  # 海外安全新闻
    WECHAT_MP = "wechat_mp"  # 微信公众号
    PAPER = "paper"  # 学术论文
    USER_UPLOAD = "user_upload"  # 用户上传文件
    WEB_SEARCH = "web_search"  # Web 搜索导入


# ═══════════════════════════════════════════════════════════
# Article 模型
# ═══════════════════════════════════════════════════════════


class ArticleBase(BaseModel):
    """文章基础模型 — API 输入/输出共用的字段"""

    url_hash: str = Field(
        ...,
        min_length=32,
        max_length=32,
        description="MD5(url) — 32 位十六进制",
        pattern=r"^[a-fA-F0-9]{32}$",
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="文章标题",
    )
    url: str = Field(
        ...,
        min_length=5,
        max_length=2048,
        description="文章原始链接",
    )
    source: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="来源名称（如 The Hacker News）",
    )
    source_type: SourceType = Field(
        default=SourceType.OVERSEAS_NEWS,
        description="来源类型",
    )

    # 时间
    published_at: datetime | None = Field(
        default=None,
        description="文章发布时间",
    )
    added_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="入库时间（UTC）",
    )

    # 内容
    summary: str = Field(default="", max_length=2000, description="原文摘要（英文）")
    summary_cn: str = Field(default="", max_length=500, description="AI 生成的中文摘要")
    content_md: str = Field(default="", max_length=50000, description="原文 Markdown")

    # 分类
    is_ai_security: bool = Field(default=False, description="是否 AI 安全相关")
    is_agent_security: bool = Field(default=False, description="是否 Agent 安全相关")
    category: str = Field(default="", max_length=100, description="分类标签（V1）")

    # V2 6分类
    category_v2: str = Field(
        default="",
        max_length=50,
        description="6分类标签（爆点事件/法律法规监管动态/AI技术重大进展/国内外竞品信息/运营商行业事件/学术会展高校）",
    )
    category_v2_confidence: int = Field(
        default=0,
        ge=0,
        le=100,
        description="6分类置信度",
    )
    category_v2_reason: str = Field(default="", max_length=100, description="6分类理由")
    category_v2_fallback: bool = Field(default=False, description="6分类是否降级")
    is_ai_agent_security_relevant: bool = Field(
        default=False,
        description="原文核心议题是否与 AI 安全或智能体安全直接相关",
    )
    ai_agent_security_relevance_confidence: int = Field(
        default=0,
        ge=0,
        le=100,
        description="AI/Agent 安全相关性判断置信度",
    )
    ai_agent_security_relevance_reason: str = Field(
        default="",
        max_length=200,
        description="AI/Agent 安全相关性判断理由",
    )
    is_pr_eligible: bool = Field(default=False, description="是否可进入PR流程（V2分类结果）")

    # V2 双维度评分
    product_relevance: int = Field(
        default=0,
        ge=0,
        le=100,
        description="V2 产品能力相关度 (0-100)",
    )
    event_impact: int = Field(
        default=0,
        ge=0,
        le=100,
        description="V2 事件影响面与传播力 (0-100)",
    )
    pr_total_score: int = Field(
        default=0,
        ge=0,
        le=200,
        description="V2 综合分 (product_relevance + event_impact)",
    )

    # V2 PR 草稿
    pr_drafts: list[dict] = Field(
        default_factory=list,
        description="V2 PR 草稿列表 [{template, perspective, content_md, title, index}, ...]",
    )
    pr_template_used: str = Field(default="", max_length=100, description="使用的模板名")

    # V1 评分（保留向后兼容）
    ai_relevance_score: int = Field(
        default=0,
        ge=0,
        le=100,
        description="V1 AI/Agent 安全相关度评分",
    )
    reportability_score: int = Field(
        default=0,
        ge=0,
        le=100,
        description="可报道性评分",
    )
    score_reason: str = Field(default="", max_length=500, description="打分理由")

    # 关联
    has_report: bool = Field(default=False, description="是否已生成 PR 报道")
    report_id: str | None = Field(default=None, description="关联的 PR 报道 ID")

    @property
    def total_score(self) -> int:
        """综合评分（0-200）"""
        return self.ai_relevance_score + self.reportability_score

    @property
    def is_high_value(self) -> bool:
        """是否高分文章（达到报道生成阈值）"""
        return self.total_score >= 140

    @field_validator("url_hash")
    @classmethod
    def validate_url_hash(cls, v: str) -> str:
        return v.lower().strip()


class ArticleInDB(ArticleBase):
    """MongoDB 中的文章文档 — 比 API 模型多了 _id"""

    id: str | None = Field(
        default=None,
        alias="_id",
        description="MongoDB ObjectId（字符串）",
    )

    model_config = {"populate_by_name": True}


class ArticleCreate(ArticleBase):
    """创建文章时的输入模型 — 只需核心字段，其余有默认值"""

    url_hash: str = Field(
        ...,
        min_length=32,
        max_length=32,
        pattern=r"^[a-fA-F0-9]{32}$",
    )
    title: str = Field(..., min_length=1, max_length=500)
    url: str = Field(..., min_length=5, max_length=2048)
    source: str = Field(..., min_length=1, max_length=200)
