"""Pydantic argument and result schemas for stage-2 business tools."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ArticleReference(ToolModel):
    article_id: str = Field(..., min_length=1, max_length=160)
    source_ref: str = Field(default="", max_length=500)
    content_hash: str = Field(default="", max_length=80)


class ArticleCandidate(ArticleReference):
    title: str = Field(default="", max_length=500)
    source: str = Field(default="", max_length=160)
    published_at: datetime | None = None
    summary: str = Field(default="", max_length=2000)
    content_available: bool = False
    untrusted_content: bool = True
    duplicate_of: str = Field(default="", max_length=160)
    score: float | None = None


class ArticleDetail(ArticleCandidate):
    content: str = Field(default="", max_length=100_000)


class ListArticlesArgs(ToolModel):
    query: str = Field(default="", max_length=500)
    source: str = Field(default="", max_length=160)
    category: str = Field(default="", max_length=160)
    published_from: datetime | None = None
    published_to: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ListArticlesResult(ToolModel):
    items: list[ArticleCandidate] = Field(default_factory=list, max_length=100)
    total: int = Field(default=0, ge=0)
    replay_ref: str = Field(default="", max_length=160)


class GetArticleArgs(ToolModel):
    article_id: str = Field(..., min_length=1, max_length=160)
    include_content: bool = True


class GetArticleResult(ToolModel):
    found: bool
    article: ArticleDetail | None = None

    @model_validator(mode="after")
    def validate_found(self) -> GetArticleResult:
        if self.found != (self.article is not None):
            raise ValueError("found must match article presence")
        return self


class SearchNewsArgs(ToolModel):
    query: str = Field(..., min_length=1, max_length=500)
    sources: list[str] = Field(default_factory=list, max_length=20)
    published_from: datetime | None = None
    published_to: datetime | None = None
    limit: int = Field(default=10, ge=1, le=50)


class SearchNewsResult(ListArticlesResult):
    query: str = Field(default="", max_length=500)


class CrawlNewsArgs(ToolModel):
    query: str = Field(default="AI security", max_length=500)
    sources: list[str] = Field(default_factory=list, max_length=20)
    published_from: datetime | None = None
    published_to: datetime | None = None
    max_results: int = Field(default=50, ge=1, le=500)
    idempotency_key: str = Field(..., min_length=8, max_length=160)

    @model_validator(mode="after")
    def validate_range(self) -> CrawlNewsArgs:
        if self.published_from and self.published_to and self.published_from > self.published_to:
            raise ValueError("published_from must be before published_to")
        return self


class CrawlNewsResult(ToolModel):
    task_ref: str = Field(..., min_length=1, max_length=160)
    status: Literal["queued", "running", "partial", "completed", "failed"] = "queued"
    added: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    articles: list[ArticleReference] = Field(default_factory=list, max_length=500)
    errors: list[str] = Field(default_factory=list, max_length=50)


class ClassifyArticleArgs(ToolModel):
    article: ArticleReference
    user_category: str = Field(default="", max_length=160)


class ClassifyArticleResult(ToolModel):
    article: ArticleReference
    category: str = Field(..., min_length=1, max_length=160)
    security_domain: str = Field(default="未知", max_length=60)
    confidence: float = Field(..., ge=0, le=1)
    reason: str = Field(default="", max_length=1000)
    eligible: bool
    conflict: str = Field(default="", max_length=500)
    model_version: str = Field(..., min_length=1, max_length=160)
    prompt_version: str = Field(..., min_length=1, max_length=160)


class MatchProductsArgs(ToolModel):
    article: ArticleReference
    explicit_product_ids: list[str] = Field(default_factory=list, max_length=5)
    max_candidates: int = Field(default=2, ge=1, le=5)


class ProductCandidate(ToolModel):
    product_id: str = Field(..., min_length=1, max_length=160)
    name: str = Field(..., min_length=1, max_length=200)
    confidence: float = Field(..., ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    user_selected: bool = False


class MatchProductsResult(ToolModel):
    article: ArticleReference
    candidates: list[ProductCandidate] = Field(default_factory=list, max_length=5)
    outcome: Literal["matched", "no_related_product", "knowledge_missing", "ambiguous"]
    conflicts: list[str] = Field(default_factory=list, max_length=20)
    catalog_hash: str = Field(..., min_length=1, max_length=100)


class ScoreArticleArgs(ToolModel):
    article: ArticleReference
    product_ids: list[str] = Field(..., min_length=1, max_length=5)
    skill_version: str = Field(default="score.v1", min_length=1, max_length=160)
    user_requested_draft: bool = False


class ScoreDimension(ToolModel):
    score: float = Field(..., ge=0, le=100)
    evidence: list[str] = Field(default_factory=list, max_length=20)


class ScoreArticleResult(ToolModel):
    article: ArticleReference
    product_relevance: ScoreDimension
    event_impact: ScoreDimension
    total_score: float = Field(..., ge=0, le=200)
    confidence: float = Field(..., ge=0, le=1)
    anomalies: list[str] = Field(default_factory=list, max_length=20)
    worth_writing: bool
    user_requested_draft: bool
    model_version: str = Field(..., min_length=1, max_length=160)
    prompt_version: str = Field(..., min_length=1, max_length=160)


class GenerateDraftArgs(ToolModel):
    article: ArticleReference
    product_ids: list[str] = Field(..., min_length=1, max_length=5)
    template_key: str = Field(default="default", min_length=1, max_length=160)
    angle: str = Field(default="", max_length=1000)
    tone: str = Field(default="professional", max_length=160)
    target_length: int = Field(default=1200, ge=200, le=10_000)
    idempotency_key: str = Field(..., min_length=8, max_length=160)


class DraftArtifact(ToolModel):
    artifact_id: str = Field(..., min_length=1, max_length=160)
    version: int = Field(..., ge=1)
    content_hash: str = Field(..., min_length=1, max_length=100)
    status: Literal["draft", "needs_review", "reviewed", "confirmed"] = "draft"


class GenerateDraftResult(ToolModel):
    artifact: DraftArtifact
    summary: str = Field(default="", max_length=2000)
    content: str = Field(default="", max_length=100_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    model_version: str = Field(..., min_length=1, max_length=160)
    prompt_version: str = Field(..., min_length=1, max_length=160)
    skill_version: str = Field(..., min_length=1, max_length=160)
    context_hash: str = Field(..., min_length=1, max_length=100)


class ReviewDraftArgs(ToolModel):
    artifact: DraftArtifact


class ReviewIssue(ToolModel):
    code: str = Field(..., min_length=1, max_length=100)
    severity: Literal["info", "warning", "error", "critical"]
    message: str = Field(..., min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


class ReviewDraftResult(ToolModel):
    artifact: DraftArtifact
    content_hash: str = Field(..., min_length=1, max_length=100)
    passed: bool
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=200)
    reviewer_version: str = Field(..., min_length=1, max_length=160)


class ReviseDraftArgs(ToolModel):
    artifact: DraftArtifact
    instruction: str = Field(..., min_length=1, max_length=4000)
    selection: str = Field(default="", max_length=20_000)
    expected_version: int = Field(..., ge=1)
    idempotency_key: str = Field(..., min_length=8, max_length=160)


class ReviseDraftResult(ToolModel):
    source_artifact: DraftArtifact
    artifact: DraftArtifact
    changed_sections: list[str] = Field(default_factory=list, max_length=100)
    review: ReviewDraftResult


class SaveDraftVersionArgs(ToolModel):
    artifact: DraftArtifact
    expected_version: int = Field(..., ge=1)
    kind: Literal["autosave", "business_version"] = "autosave"
    confirmed_by_user: bool = False
    idempotency_key: str = Field(..., min_length=8, max_length=160)

    @model_validator(mode="after")
    def require_confirmation(self) -> SaveDraftVersionArgs:
        if self.kind == "business_version" and not self.confirmed_by_user:
            raise ValueError("business_version requires explicit user confirmation")
        return self


class SaveDraftVersionResult(ToolModel):
    artifact: DraftArtifact
    saved: bool
    kind: Literal["autosave", "business_version"]
    duplicate: bool = False


class ExportDraftArgs(ToolModel):
    artifact: DraftArtifact
    format: Literal["markdown", "docx", "pdf"] = "markdown"
    filename: str = Field(default="draft", min_length=1, max_length=180)
    idempotency_key: str = Field(..., min_length=8, max_length=160)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if any(part in value for part in ("..", "/", "\\", "\x00")):
            raise ValueError("filename contains an unsafe path component")
        return value


class ExportDraftResult(ToolModel):
    artifact: DraftArtifact
    export_ref: str = Field(..., min_length=1, max_length=500)
    format: Literal["markdown", "docx", "pdf"]
    content_hash: str = Field(..., min_length=1, max_length=100)
    immutable: bool = True


BusinessToolArgs = (
    ListArticlesArgs
    | GetArticleArgs
    | SearchNewsArgs
    | CrawlNewsArgs
    | ClassifyArticleArgs
    | MatchProductsArgs
    | ScoreArticleArgs
    | GenerateDraftArgs
    | ReviewDraftArgs
    | ReviseDraftArgs
    | SaveDraftVersionArgs
    | ExportDraftArgs
)


def model_payload(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    return value.model_dump(mode="python") if isinstance(value, BaseModel) else dict(value)
