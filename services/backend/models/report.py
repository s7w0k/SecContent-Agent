"""
PR Report MongoDB 数据模型 — Agent 生成的 PR 情报报道。

结构化的 Markdown 报道，关联回原始文章。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════
# 报道模板枚举
# ═══════════════════════════════════════════════════════════


class ReportTemplate(str):
    """PR 报道模板类型"""

    STANDARD = "standard_pr"  # 标准 PR 情报模板
    BRIEF = "brief"  # 简报（短格式）
    DEEP_DIVE = "deep_dive"  # 深度分析


# ═══════════════════════════════════════════════════════════
# Report 模型
# ═══════════════════════════════════════════════════════════


class ReportBase(BaseModel):
    """PR 报道基础模型"""

    article_url_hash: str = Field(
        ...,
        min_length=32,
        max_length=32,
        description="关联文章的 url_hash",
        pattern=r"^[a-f0-9]{32}$",
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description="报道标题",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="生成时间（UTC）",
    )
    template: str = Field(
        default="standard_pr",
        description="使用的报道模板",
    )
    content_md: str = Field(
        default="",
        max_length=50000,
        description="报道正文（Markdown）",
    )

    # 评分快照（生成时的评分，可能后续变化）
    scores: dict = Field(
        default_factory=lambda: {"relevance": 0, "reportability": 0},
        description="生成时的文章评分快照",
    )

    # 元信息
    source_article_title: str = Field(
        default="",
        max_length=500,
        description="原始文章标题（便于阅读）",
    )
    source_article_url: str = Field(
        default="",
        max_length=2048,
        description="原始文章链接",
    )
    generated_by: str = Field(
        default="pr-agent-pipeline",
        description="生成者标识（流水线版本）",
    )


class ReportInDB(ReportBase):
    """MongoDB 中的报道文档"""

    id: str | None = Field(
        default=None,
        alias="_id",
        description="MongoDB ObjectId（字符串）",
    )

    model_config = {"populate_by_name": True}


class ReportCreate(BaseModel):
    """创建报道时的输入模型"""

    article_url_hash: str = Field(..., min_length=32, max_length=32)
    title: str = Field(..., min_length=1, max_length=300)
    content_md: str = Field(..., max_length=50000)
    template: str = Field(default="standard_pr")
    scores: dict = Field(default_factory=dict)
    source_article_title: str = Field(default="", max_length=500)
    source_article_url: str = Field(default="", max_length=2048)
