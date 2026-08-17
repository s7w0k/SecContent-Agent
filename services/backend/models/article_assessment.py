"""用户级文章判断模型 (user_article_assessments)。

保存每个用户对每篇文章的分类、评分、候选判断和配置指纹。
与共享 articles 集合分离，实现多租户数据隔离。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from models.generation_config import ProductRoutingSnapshot
from pydantic import BaseModel, Field


class ClassificationAssessment(BaseModel):
    """分类判断。"""

    category_v2: str = ""
    confidence: int = 0
    reason: str = ""
    is_ai_agent_security_relevant: bool = False
    is_pr_eligible: bool = False


class ScoringAssessment(BaseModel):
    """评分判断。"""

    score_mode: str = "product_event"
    product_relevance: int | None = None
    event_impact: int = 0
    candidate_score: int = 0
    candidate_threshold: int = 80
    is_pr_candidate: bool = False
    reason: str = ""
    tags: list[str] = Field(default_factory=list)


class ProductSnapshot(BaseModel):
    """产品选择快照。"""

    mode: str = "auto"
    requested_product_ids: list[str] = Field(default_factory=list)
    resolved_products: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_hash: str = ""
    # 阶段0：用户级单篇评分同时保存路由快照副本或稳定引用
    routing: ProductRoutingSnapshot | None = None


class PromptRefSnapshot(BaseModel):
    """提示词版本快照。"""

    prompt_key: str
    source: str
    version: int
    content_hash: str


class UserArticleAssessment(BaseModel):
    """用户级文章判断文档模型。"""

    assessment_id: str = ""
    user_id: str
    article_url_hash: str
    version: int = 1
    classification: ClassificationAssessment = Field(default_factory=ClassificationAssessment)
    scoring: ScoringAssessment = Field(default_factory=ScoringAssessment)
    product_snapshot: ProductSnapshot = Field(default_factory=ProductSnapshot)
    prompt_refs: list[PromptRefSnapshot] = Field(default_factory=list)
    input_fingerprint: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


def compute_input_fingerprint(
    *,
    user_id: str,
    article_url_hash: str,
    prompt_refs: list[dict[str, Any]],
    product_snapshot: dict[str, Any],
    knowledge_hash: str,
) -> str:
    """计算输入指纹（用于幂等复用判断）。"""
    import hashlib
    import json

    parts = {
        "user_id": user_id,
        "article_url_hash": article_url_hash,
        "prompt_refs": sorted(prompt_refs, key=lambda x: x.get("prompt_key", "")),
        "product_mode": product_snapshot.get("mode", ""),
        "product_ids": sorted(product_snapshot.get("requested_product_ids", [])),
        "knowledge_hash": knowledge_hash,
    }
    content = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
