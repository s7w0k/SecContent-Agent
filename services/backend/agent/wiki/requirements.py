"""Evidence Requirement Planner（Phase 7 / PR-13，§10.2/§10.4）。

Navigator V2 由"按预算遍历"升级为 requirement-driven：
  - 每个任务类型声明一组 EvidenceRequirement（requirement_id / description /
    weight / required_page_types / minimum_evidence / status）
  - RequirementTracker 依据已打开页面的 page_type 累计覆盖，供 Stop Condition
    （§10.7）判断 SUFFICIENT。

Score 任务示例（§10.4）：
  R1 能力对应 weight=0.5
  R2 场景匹配 weight=0.3
  R3 限制/反例 weight=0.2
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RequirementStatus(StrEnum):
    OPEN = "OPEN"
    MET = "MET"


class TaskType(StrEnum):
    SCORE = "score"
    DRAFT = "draft"
    CHAT = "chat"


class EvidenceRequirement(BaseModel):
    """一条证据需求。"""

    requirement_id: str
    description: str
    weight: float = Field(ge=0.0, le=1.0)
    required_page_types: list[str] = Field(default_factory=list)
    minimum_evidence: int = Field(default=1, ge=1)
    status: RequirementStatus = RequirementStatus.OPEN


_DEFAULT_REQUIREMENTS: dict[str, list[dict]] = {
    "score": [
        {
            "requirement_id": "R1",
            "description": "产品是否具备与事件对应的能力？",
            "weight": 0.5,
            "required_page_types": ["product", "capability"],
            "minimum_evidence": 1,
        },
        {
            "requirement_id": "R2",
            "description": "能力适用场景是否匹配？",
            "weight": 0.3,
            "required_page_types": ["scenario"],
            "minimum_evidence": 1,
        },
        {
            "requirement_id": "R3",
            "description": "是否有明确限制/反例？",
            "weight": 0.2,
            "required_page_types": ["limitation"],
            "minimum_evidence": 1,
        },
    ],
    "draft": [
        {
            "requirement_id": "D1",
            "description": "产品定位与核心能力",
            "weight": 0.4,
            "required_page_types": ["product", "positioning", "capability"],
            "minimum_evidence": 1,
        },
        {
            "requirement_id": "D2",
            "description": "典型使用场景与集成方式",
            "weight": 0.4,
            "required_page_types": ["scenario", "integration"],
            "minimum_evidence": 1,
        },
        {
            "requirement_id": "D3",
            "description": "限制/边界信息",
            "weight": 0.2,
            "required_page_types": ["limitation"],
            "minimum_evidence": 1,
        },
    ],
    "chat": [
        {
            "requirement_id": "C1",
            "description": "回答所需的直接知识",
            "weight": 0.7,
            "required_page_types": ["product", "capability", "scenario"],
            "minimum_evidence": 1,
        },
        {
            "requirement_id": "C2",
            "description": "补充/反例知识",
            "weight": 0.3,
            "required_page_types": ["limitation"],
            "minimum_evidence": 1,
        },
    ],
}


def default_requirements(task_type: str) -> list[EvidenceRequirement]:
    """返回某任务类型的默认 EvidenceRequirement 列表。"""
    conf = _DEFAULT_REQUIREMENTS.get(task_type, _DEFAULT_REQUIREMENTS["score"])
    return [EvidenceRequirement(**item) for item in conf]


class RequirementTracker:
    """依据已打开页面累计 requirement 覆盖（§10.2/§10.4）。"""

    def __init__(self, requirements: list[EvidenceRequirement] | None = None):
        self.requirements = list(requirements or [])
        self._evidence_by_requirement: dict[str, int] = {
            r.requirement_id: 0 for r in self.requirements
        }

    @property
    def met(self) -> list[str]:
        return [
            r.requirement_id
            for r in self.requirements
            if self._evidence_by_requirement[r.requirement_id] >= r.minimum_evidence
        ]

    @property
    def missing(self) -> list[str]:
        return [
            r.requirement_id
            for r in self.requirements
            if self._evidence_by_requirement[r.requirement_id] < r.minimum_evidence
        ]

    def coverage(self) -> float:
        if not self.requirements:
            return 0.0
        total_weight = sum(r.weight for r in self.requirements)
        if total_weight <= 0:
            return 0.0
        earned = sum(r.weight if r.requirement_id in self.met else 0.0 for r in self.requirements)
        return earned / total_weight

    def observe_page(self, page: Any) -> None:
        """按 page_type 计入对应 requirement 的 evidence 数量。"""
        page_type = str(getattr(getattr(page, "meta", page), "page_type", ""))
        for r in self.requirements:
            if page_type and page_type in r.required_page_types:
                self._evidence_by_requirement[r.requirement_id] += 1

    def snapshot(self) -> dict:
        return {
            "requirements": [r.model_dump() for r in self.requirements],
            "evidence_by_requirement": dict(self._evidence_by_requirement),
            "met": self.met,
            "missing": self.missing,
            "coverage": self.coverage(),
        }
