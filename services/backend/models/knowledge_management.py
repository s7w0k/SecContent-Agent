"""知识库管理数据模型。

包含草稿、修订、发布、审计日志等知识库管理所需的 Pydantic 模型。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ── 枚举 ──────────────────────────────────────────────


class KnowledgeDraftStatus(StrEnum):
    """知识库草稿状态。"""

    EDITING = "editing"
    VALIDATED = "validated"
    PUBLISHED = "published"
    ABANDONED = "abandoned"


class KnowledgePublicationStatus(StrEnum):
    """知识库发布状态。"""

    VALIDATING = "validating"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class KnowledgeValidationStatus(StrEnum):
    """校验结果状态。"""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


# ── 校验结果 ──────────────────────────────────────────


class KnowledgeValidationResult(BaseModel):
    """草稿或发布前的内容校验结果。"""

    status: KnowledgeValidationStatus = KnowledgeValidationStatus.PENDING
    errors: list[str] = Field(default_factory=list, description="校验错误列表")
    warnings: list[str] = Field(default_factory=list, description="校验告警列表")


# ── 草稿 ──────────────────────────────────────────────


class KnowledgeDraftCreate(BaseModel):
    """创建知识库草稿请求。"""

    document_id: str = Field(description="关联知识库文档 ID")
    base_content_hash: str = Field(description="草稿基线内容哈希")


class KnowledgeDraftUpdate(BaseModel):
    """更新知识库草稿请求。"""

    content_md: str = Field(description="新的 Markdown 内容")
    change_summary: str = Field(default="", description="本次变更摘要")


class KnowledgeDraftInDB(BaseModel):
    """MongoDB knowledge_drafts 集合文档。"""

    id: str | None = Field(default=None, alias="_id")
    draft_id: str = Field(description="草稿唯一 ID")
    document_id: str = Field(description="关联知识库文档 ID")
    relative_path: str = Field(description="文档相对路径")
    base_content_hash: str = Field(description="基线内容哈希")
    content_md: str = Field(description="当前 Markdown 内容")
    status: KnowledgeDraftStatus = KnowledgeDraftStatus.EDITING
    validation: KnowledgeValidationResult = Field(default_factory=KnowledgeValidationResult)
    change_summary: str = Field(default="", description="本次变更摘要")
    created_by: str = Field(description="创建者 user_id")
    updated_by: str = Field(description="最近更新者 user_id")
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    model_config = {"populate_by_name": True}


class KnowledgeDraftResponse(BaseModel):
    """草稿 API 响应。"""

    draft_id: str
    document_id: str
    relative_path: str
    base_content_hash: str
    content_md: str
    status: KnowledgeDraftStatus
    validation: KnowledgeValidationResult
    change_summary: str = ""
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


# ── 修订 ──────────────────────────────────────────────


class KnowledgeRevision(BaseModel):
    """发布版本中单文件的修订记录。"""

    revision_id: str = Field(description="修订唯一 ID")
    publication_id: str = Field(description="所属发布 ID")
    draft_id: str = Field(default="", description="来源草稿 ID")
    relative_path: str = Field(description="文档相对路径")
    previous_content_hash: str | None = Field(default=None, description="前一版本内容哈希")
    new_content_hash: str = Field(default="", description="新内容哈希")
    diff_summary: str = Field(default="", description="变更摘要")
    before_content: str = Field(default="", description="发布前内容快照（用于回滚）")
    after_content: str = Field(default="", description="发布后内容快照")
    change_summary: str = Field(default="", description="变更摘要（来自草稿）")
    published_by: str = Field(default="", description="发布者 user_id")
    published_at: datetime | None = Field(default=None, description="发布时间")
    created_at: datetime = Field(default_factory=_utc_now)


# ── 发布 ──────────────────────────────────────────────


class KnowledgePublicationFile(BaseModel):
    """发布版本中的单个文件快照。"""

    relative_path: str = Field(description="文档相对路径")
    content_hash: str = Field(default="", description="发布内容哈希")
    revision_id: str = Field(default="", description="关联修订 ID")
    before_hash: str = Field(default="", description="发布前内容哈希")
    after_hash: str = Field(default="", description="发布后内容哈希")


class KnowledgePublicationCreate(BaseModel):
    """创建知识库发布请求。"""

    draft_ids: list[str] = Field(min_length=1, description="待发布草稿 ID 列表")
    version_name: str = Field(description="版本名称")
    release_notes: str = Field(default="", description="发布说明")


class KnowledgePublicationInDB(BaseModel):
    """MongoDB knowledge_publications 集合文档。"""

    id: str | None = Field(default=None, alias="_id")
    publication_id: str = Field(description="发布唯一 ID")
    version_name: str = Field(description="版本名称")
    release_notes: str = Field(default="", description="发布说明")
    status: KnowledgePublicationStatus = KnowledgePublicationStatus.VALIDATING
    files: list[KnowledgePublicationFile] = Field(default_factory=list, description="发布文件列表")
    validation: KnowledgeValidationResult = Field(default_factory=KnowledgeValidationResult)
    knowledge_hash_before: str = Field(default="", description="发布前知识库联合哈希")
    knowledge_hash_after: str = Field(default="", description="发布后知识库联合哈希")
    index_version: str = Field(default="", description="发布后知识索引版本")
    rollback_of: str | None = Field(default=None, description="回滚目标发布 ID")
    published_by: str = Field(description="发布者 user_id")
    published_at: datetime | None = Field(default=None, description="发布完成时间")
    rolled_back_at: datetime | None = Field(default=None, description="回滚时间")
    rollback_reason: str | None = Field(default=None, description="回滚原因")
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    model_config = {"populate_by_name": True}


class KnowledgePublicationResponse(BaseModel):
    """发布 API 响应。"""

    publication_id: str
    version_name: str
    release_notes: str
    status: KnowledgePublicationStatus
    files: list[KnowledgePublicationFile]
    validation: KnowledgeValidationResult
    knowledge_hash_before: str = ""
    knowledge_hash_after: str = ""
    index_version: str = ""
    rollback_of: str | None = None
    published_by: str
    published_at: datetime | None
    rolled_back_at: datetime | None
    rollback_reason: str | None
    created_at: datetime
    updated_at: datetime


# ── 回滚 ──────────────────────────────────────────────


class KnowledgeRollbackRequest(BaseModel):
    """知识库发布回滚请求。"""

    reason: str = Field(min_length=1, description="回滚原因")


# ── 审计日志 ──────────────────────────────────────────


class KnowledgeAuditLog(BaseModel):
    """知识库管理操作审计日志。"""

    audit_id: str = Field(description="审计记录唯一 ID")
    user_id: str = Field(description="操作者 user_id")
    action: str = Field(description="操作类型，如 draft.create / publication.publish")
    target_type: str = Field(description="操作目标类型，如 draft / publication")
    target_id: str = Field(description="操作目标 ID")
    detail: dict[str, Any] = Field(default_factory=dict, description="附加详情")
    created_at: datetime = Field(default_factory=_utc_now)
