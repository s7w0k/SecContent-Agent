"""用户记忆与个性化偏好数据模型。

包含显式偏好 Policy、记忆事件、原子记忆、场景摘要和 Memory Pack。
所有模型遵循多租户隔离约束（user_id 为必填字段）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


# ── 枚举 ──────────────────────────────────────────────


class MemoryDimension(StrEnum):
    TONE = "tone"
    LENGTH = "length"
    TEMPLATE = "template"
    PERSPECTIVE = "perspective"
    STRUCTURE = "structure"
    TITLE_STYLE = "title_style"
    CONTENT_ORDER = "content_order"
    REVISE_DIRECTION = "revise_direction"
    AVOID_PATTERN = "avoid_pattern"
    REQUIRED_PATTERN = "required_pattern"


class MemoryPolarity(StrEnum):
    PREFER = "prefer"
    AVOID = "avoid"
    REQUIRE = "require"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"
    SUPPRESSED = "suppressed"
    EXPIRED = "expired"
    REJECTED = "rejected"


class MemorySourceType(StrEnum):
    EXPLICIT_POLICY = "explicit_policy"
    EXPLICIT_CORRECTION = "explicit_correction"
    FEEDBACK_RATING = "feedback_rating"
    FEEDBACK_COMMENT = "feedback_comment"
    REVISION_REQUEST = "revision_request"
    REVISION_APPLY = "revision_apply"
    FINAL_DIFF = "final_diff"
    DRAFT_DOWNLOAD = "draft_download"
    PERSONALIZATION_FEEDBACK = "personalization_feedback"


class MemoryStage(StrEnum):
    DRAFT = "draft"
    REVISE = "revise"
    REVIEW = "review"


class MemoryEventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# ── 显式偏好 Policy ────────────────────────────────────


class CategoryOverride(BaseModel):
    """分类级别的偏好覆盖。"""

    preferred_tone: str | None = None
    preferred_length: str | None = None


class ProfilePolicy(BaseModel):
    """用户显式偏好策略，作为自动学习不能覆盖的权威层。"""

    policy_id: str
    user_id: str
    preferred_tone: str | None = None
    preferred_length: str | None = None
    target_audience: list[str] = Field(default_factory=list)
    required_patterns: list[str] = Field(default_factory=list, max_length=20)
    avoid_patterns: list[str] = Field(default_factory=list, max_length=20)
    category_overrides: dict[str, CategoryOverride] = Field(default_factory=dict)
    auto_learning_enabled: bool = True
    memory_write_approval: bool = True
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── 记忆事件 ──────────────────────────────────────────


class MemoryEvent(BaseModel):
    """统一承载反馈和行为信号，实现幂等异步学习。"""

    event_id: str
    idempotency_key: str
    user_id: str
    source_type: MemorySourceType
    source_id: str = ""
    article_url_hash: str | None = None
    draft_index: int | None = None
    revision_id: str | None = None
    generation_id: str | None = None
    template_id: str | None = None
    template_key: str | None = None
    template_version: int | None = None
    category_v2: str | None = None
    stage: MemoryStage = MemoryStage.DRAFT
    payload: dict[str, Any] = Field(default_factory=dict)
    status: MemoryEventStatus = MemoryEventStatus.PENDING
    attempts: int = 0
    candidate_memory_ids: list[str] = Field(default_factory=list)
    processor_version: str = "memory-learner-v1"
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    processed_at: datetime | None = None


# ── 原子记忆 ──────────────────────────────────────────


class MemoryScope(BaseModel):
    """记忆适用范围。"""

    category_v2: str | None = None
    template_id: str | None = None
    stage: MemoryStage | None = None
    target_audience: str | None = None


class MemoryEvidence(BaseModel):
    """记忆证据引用。"""

    event_id: str
    source_type: MemorySourceType
    weight: float = Field(ge=0, le=1)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryItem(BaseModel):
    """可独立更新、可审计的原子偏好。"""

    memory_id: str
    user_id: str
    dimension: MemoryDimension
    value: str
    normalized_key: str
    display_text: str
    polarity: MemoryPolarity
    scope: MemoryScope = Field(default_factory=MemoryScope)
    confidence: float = Field(default=0.0, ge=0, le=1)
    support_count: int = 0
    contradiction_count: int = 0
    independent_task_count: int = 0
    evidence_refs: list[MemoryEvidence] = Field(default_factory=list, max_length=20)
    status: MemoryStatus = MemoryStatus.CANDIDATE
    created_by: str = "auto"  # "auto" | "user"
    confirmed_by_user: bool = False
    suppressed_by: str | None = None
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = None
    use_count: int = 0
    positive_outcome_count: int = 0
    negative_outcome_count: int = 0
    expires_at: datetime | None = None
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── 场景摘要 ──────────────────────────────────────────


class SoftPreference(BaseModel):
    """软偏好条目。"""

    memory_id: str
    text: str
    confidence: float = Field(ge=0, le=1)


class MemorySummary(BaseModel):
    """有界、按场景编译的热记忆摘要。"""

    summary_id: str
    user_id: str
    scope_key: str  # e.g. "law_policy:draft"
    scope: MemoryScope
    policy_version: int = 1
    memory_item_ids: list[str] = Field(default_factory=list)
    hard_preferences: list[str] = Field(default_factory=list)
    soft_preferences: list[SoftPreference] = Field(default_factory=list)
    avoid_patterns: list[str] = Field(default_factory=list)
    rendered_text: str = ""
    char_count: int = 0
    compiler_version: str = "memory-compiler-v1"
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Memory Pack ───────────────────────────────────────


class MemoryPack(BaseModel):
    """冻结的检索结果，用于注入生成上下文。"""

    user_id: str
    scope_key: str
    policy: ProfilePolicy | None = None
    memory_items: list[MemoryItem] = Field(default_factory=list)
    hard_preferences: list[str] = Field(default_factory=list)
    soft_preferences: list[SoftPreference] = Field(default_factory=list)
    avoid_patterns: list[str] = Field(default_factory=list)
    rendered_text: str = ""
    char_count: int = 0
    item_count: int = 0
    pruned_count: int = 0
    experiment: dict[str, str] = Field(default_factory=dict)
