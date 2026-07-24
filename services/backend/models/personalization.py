"""生成归因与个性化演进数据模型。

包含 GenerationRun（生成快照）和 PersonalizationCandidate（离线候选策略）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


# ── 枚举 ──────────────────────────────────────────────


class GenerationStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class CandidateStatus(StrEnum):
    DRAFT = "draft"
    EVALUATING = "evaluating"
    GATE_FAILED = "gate_failed"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVE = "active"
    RETIRED = "retired"
    ROLLED_BACK = "rolled_back"


# ── 生成归因 ──────────────────────────────────────────


class MemoryPackSnapshot(BaseModel):
    """生成时使用的 Memory Pack 快照（去标识化）。"""

    hard_preferences: list[str] = Field(default_factory=list)
    soft_preferences: list[dict[str, Any]] = Field(default_factory=list)
    avoid_patterns: list[str] = Field(default_factory=list)
    rendered_char_count: int = 0


class ReviewSummary(BaseModel):
    """审核结果摘要。"""

    status: str = "pending"
    high: int = 0
    medium: int = 0
    low: int = 0


class GenerationOutcome(BaseModel):
    """用户行为结果回流。"""

    viewed: bool = False
    downloaded: bool = False
    feedback_rating: int | None = None
    revision_requested: bool = False
    revision_applied: bool = False
    personalization_feedback: str | None = None


class ExperimentInfo(BaseModel):
    """实验分组信息。"""

    experiment_id: str = ""
    group: str = "control"  # "control" | "treatment"


class GenerationRun(BaseModel):
    """保存个性化输入快照和结果归因。

    在调用 LLM 前创建（status=running），生成后更新结果。
    保留期默认 180 天。
    """

    generation_id: str
    trace_id: str = ""
    task_id: str = ""
    user_id: str
    article_url_hash: str = ""
    draft_index: int = 0
    stage: str = "draft"
    category_v2: str | None = None
    template_id: str | None = None
    template_key: str | None = None
    template_version: int | None = None
    profile_policy_version: int | None = None
    memory_summary_version: int | None = None
    memory_item_ids: list[str] = Field(default_factory=list)
    memory_pack_snapshot: MemoryPackSnapshot = Field(default_factory=MemoryPackSnapshot)
    system_prompt_version: str = ""
    system_prompt_hash: str = ""
    custom_prompt_version: int | None = None
    reference_template_hash: str = ""
    model_name: str = ""
    experiment: ExperimentInfo = Field(default_factory=ExperimentInfo)
    generation_status: GenerationStatus = GenerationStatus.RUNNING
    review: ReviewSummary = Field(default_factory=ReviewSummary)
    outcomes: GenerationOutcome = Field(default_factory=GenerationOutcome)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── 离线候选策略 ────────────────────────────────────────


class CandidateEvaluation(BaseModel):
    """候选策略评测结果。"""

    dataset_id: str = ""
    train_metrics: dict[str, float] = Field(default_factory=dict)
    val_metrics: dict[str, float] = Field(default_factory=dict)
    holdout_metrics: dict[str, float] = Field(default_factory=dict)
    category_metrics: dict[str, dict[str, float]] = Field(default_factory=dict)
    gate_results: dict[str, bool] = Field(default_factory=dict)
    cost_per_generation: float = 0.0
    latency_p95_ms: int = 0


class PersonalizationCandidate(BaseModel):
    """离线演进候选和审批状态。

    状态流转：draft -> evaluating -> gate_failed|ready_for_review -> approved -> shadow -> canary -> active -> retired|rolled_back
    未审批候选不能进入 Active。
    """

    candidate_id: str
    target_type: str  # e.g. "memory_renderer", "retrieval_weights", "template_ranker"
    base_version: str
    candidate_version: str
    content: dict[str, Any] = Field(default_factory=dict)
    source_dataset_id: str = ""
    status: CandidateStatus = CandidateStatus.DRAFT
    metrics: CandidateEvaluation = Field(default_factory=CandidateEvaluation)
    created_by: str = "offline-evaluator"
    approved_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
