"""用户反馈、操作记录与个性化风格画像 MongoDB 数据模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

UrlHash = Annotated[str, Field(pattern=r"^[a-fA-F0-9]{32}$")]
RatingValue = Annotated[int, Field(ge=1, le=5)]
FeedbackTag = Annotated[str, Field(min_length=1, max_length=100)]


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)


def _uuid() -> str:
    """生成适合 MongoDB 文档存储的 UUID 字符串。"""
    return str(uuid4())


class TargetType(StrEnum):
    """反馈对象类型。"""

    DRAFT = "draft"
    REVISION = "revision"
    ARTICLE_SCORE = "article_score"
    PIPELINE = "pipeline"


class ActionType(StrEnum):
    """需要记录的用户操作类型。"""

    DRAFT_VIEW = "draft_view"
    DRAFT_DOWNLOAD = "draft_download"
    DRAFT_REVISE = "draft_revise"
    REVISION_APPLY = "revision_apply"
    FEEDBACK_SUBMIT = "feedback_submit"
    PIPELINE_RUN = "pipeline_run"


class FeedbackStatus(StrEnum):
    """反馈记录状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


class PreferredLength(StrEnum):
    """用户偏好的稿件篇幅。"""

    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class PreferredTone(StrEnum):
    """用户偏好的稿件语气。"""

    MARKET_ORIENTED = "market_oriented"
    TECHNICAL = "technical"
    EXECUTIVE = "executive"


class FeedbackTargetRef(BaseModel):
    """反馈对象定位信息。"""

    article_url_hash: UrlHash
    draft_index: int | None = Field(default=None, ge=0)
    revision_id: str | None = Field(default=None, min_length=1, max_length=100)
    pipeline_id: str | None = Field(default=None, min_length=1, max_length=100)


class ActivityTarget(BaseModel):
    """操作记录的目标对象。"""

    article_url_hash: UrlHash | None = None
    draft_index: int | None = Field(default=None, ge=0)
    template: str | None = Field(default=None, max_length=100)
    perspective: str | None = Field(default=None, max_length=200)
    revision_id: str | None = Field(default=None, min_length=1, max_length=100)
    pipeline_id: str | None = Field(default=None, min_length=1, max_length=100)


class FeedbackCreate(BaseModel):
    """提交反馈时的输入模型。"""

    target_type: TargetType
    target_ref: FeedbackTargetRef
    rating: RatingValue
    rating_dimensions: dict[str, RatingValue] | None = None
    comment: str = Field(default="", max_length=2000)
    tags: list[FeedbackTag] = Field(default_factory=list, max_length=20)


class FeedbackUpdate(BaseModel):
    """更新反馈时的输入模型。"""

    rating: RatingValue | None = None
    rating_dimensions: dict[str, RatingValue] | None = None
    comment: str | None = Field(default=None, max_length=2000)
    tags: list[FeedbackTag] | None = Field(default=None, max_length=20)
    status: FeedbackStatus | None = None


class Feedback(FeedbackCreate):
    """MongoDB 中的用户反馈文档。"""

    id: str | None = Field(default=None, alias="_id")
    feedback_id: str = Field(default_factory=_uuid)
    user_id: str = Field(default="local-user", min_length=1, max_length=100)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    status: FeedbackStatus = FeedbackStatus.ACTIVE

    model_config = {"populate_by_name": True}


class UserActivityCreate(BaseModel):
    """记录用户操作时的输入模型。"""

    action: ActionType
    target: ActivityTarget
    context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_target(self) -> UserActivityCreate:
        """非流水线操作必须关联文章。"""
        if self.action != ActionType.PIPELINE_RUN and not self.target.article_url_hash:
            raise ValueError("article_url_hash is required for this action")
        return self


class UserActivity(UserActivityCreate):
    """MongoDB 中的用户操作记录。"""

    id: str | None = Field(default=None, alias="_id")
    activity_id: str = Field(default_factory=_uuid)
    user_id: str = Field(default="local-user", min_length=1, max_length=100)
    created_at: datetime = Field(default_factory=_utc_now)

    model_config = {"populate_by_name": True}


class StyleHints(BaseModel):
    """可注入草稿生成 Prompt 的用户风格偏好。"""

    preferred_templates: list[str] = Field(default_factory=list)
    preferred_perspectives: list[str] = Field(default_factory=list)
    preferred_length: PreferredLength = PreferredLength.MEDIUM
    preferred_tone: PreferredTone = PreferredTone.MARKET_ORIENTED
    common_revise_directions: list[str] = Field(default_factory=list)
    avoid_patterns: list[str] = Field(default_factory=list)


class PreferenceMetric(BaseModel):
    """单个模板或视角的偏好统计。"""

    count: int = Field(default=0, ge=0)
    avg_rating: float = Field(default=0, ge=0, le=5)
    download_count: int = Field(default=0, ge=0)
    apply_count: int = Field(default=0, ge=0)
    revise_count: int = Field(default=0, ge=0)


class PreferenceScores(BaseModel):
    """模板和视角的偏好分组统计。"""

    template_scores: dict[str, PreferenceMetric] = Field(default_factory=dict)
    perspective_scores: dict[str, PreferenceMetric] = Field(default_factory=dict)


class FeedbackSummary(BaseModel):
    """用户反馈汇总。"""

    total_feedbacks: int = Field(default=0, ge=0)
    avg_rating: float = Field(default=0, ge=0, le=5)
    positive_count: int = Field(default=0, ge=0)
    negative_count: int = Field(default=0, ge=0)
    neutral_count: int = Field(default=0, ge=0)
    top_tags: list[str] = Field(default_factory=list)


class ActivitySummary(BaseModel):
    """用户操作汇总。"""

    total_downloads: int = Field(default=0, ge=0)
    total_applies: int = Field(default=0, ge=0)
    total_revises: int = Field(default=0, ge=0)
    total_feedbacks: int = Field(default=0, ge=0)
    last_active_at: datetime | None = None


class ReviseInstructionPattern(BaseModel):
    """从历史改稿指令中提取的模式。"""

    pattern: str = Field(..., min_length=1, max_length=200)
    count: int = Field(default=0, ge=0)


class StyleProfile(BaseModel):
    """MongoDB 中的用户个性化风格画像。"""

    id: str | None = Field(default=None, alias="_id")
    user_id: str = Field(default="local-user", min_length=1, max_length=100)
    style_hints: StyleHints = Field(default_factory=StyleHints)
    preference_scores: PreferenceScores = Field(default_factory=PreferenceScores)
    feedback_summary: FeedbackSummary = Field(default_factory=FeedbackSummary)
    activity_summary: ActivitySummary = Field(default_factory=ActivitySummary)
    revise_instruction_patterns: list[ReviseInstructionPattern] = Field(default_factory=list)
    llm_analysis: str = Field(default="", max_length=5000)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    model_config = {"populate_by_name": True}
