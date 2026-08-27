"""Artifact Layer 契约 - ArtifactRef / AgentHandoff（计划 §45 / §46 / §47）。

Agent / Skill 之间不传大文本，只传 ArtifactRef；产物以"类"区分，
由 ArtifactStore 版本化 + 内容哈希 + Producer 标识地持久化。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

# 计划 §45 建议的产物类型白名单。
ArtifactType = Literal[
    "ArticleArtifact",
    "TriageArtifact",
    "EvidenceBundleArtifact",
    "ScoringArtifact",
    "DraftArtifact",
    "ReviewArtifact",
    "RevisionInstructionArtifact",
    "MaintenanceProposalArtifact",
    "WikiEvaluationArtifact",
    "SkillArtifact",
]


class ArtifactRef(BaseModel):
    """计划 §46：不可变的产物句柄。"""

    artifact_id: str
    artifact_type: ArtifactType
    version: int = Field(default=1, ge=1)
    content_hash: str = ""
    producer: str = ""
    run_id: str = ""
    step_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def ref(self) -> str:
        """紧凑引用字符串，用于 in-context 传递。"""
        return f"{self.artifact_type}:{self.artifact_id}@{self.version}"


class AgentHandoff(BaseModel):
    """计划 §47：Agent 之间的受控交接（禁止完整 Chat History / 整个 Wiki）。"""

    from_agent: str
    to_agent: str
    goal: str
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    expected_output_type: str = ""


__all__ = [
    "AgentHandoff",
    "ArtifactRef",
    "ArtifactType",
]
