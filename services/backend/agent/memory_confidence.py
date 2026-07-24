"""记忆置信度计算。

第一版采用确定性公式，不让 LLM 直接决定最终置信度。

confidence =
    clamp(support_score - 0.60 × conflict_score + confirmation_bonus + repeated_task_bonus, 0, 1)
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

from config import get_settings
from models.memory import MemoryEvidence, MemorySourceType, MemoryStatus

logger = logging.getLogger("backend.agent.memory_confidence")

# 信号权重映射
SOURCE_WEIGHTS: dict[MemorySourceType, float] = {
    MemorySourceType.EXPLICIT_POLICY: 1.00,
    MemorySourceType.EXPLICIT_CORRECTION: 0.95,
    MemorySourceType.REVISION_APPLY: 0.90,
    MemorySourceType.FINAL_DIFF: 0.85,
    MemorySourceType.FEEDBACK_COMMENT: 0.80,
    MemorySourceType.FEEDBACK_RATING: 0.75,
    MemorySourceType.PERSONALIZATION_FEEDBACK: 0.75,
    MemorySourceType.DRAFT_DOWNLOAD: 0.35,
    MemorySourceType.REVISION_REQUEST: 0.10,
}


def _time_decay(observed_at: datetime, half_life_days: int) -> float:
    """时间衰减因子：0.5 ^ (age_days / half_life_days)"""
    age_days = (datetime.now(UTC) - observed_at).total_seconds() / 86400
    return 0.5 ** (age_days / max(half_life_days, 1))


def _independence_factor(evidence_refs: list[MemoryEvidence]) -> float:
    """独立性因子：简化版，基于证据数量估计。

    1.0  不同文章或不同任务（≥3 条独立证据）
    0.6  中等独立（2 条）
    0.3  单一来源（1 条）
    """
    count = len(evidence_refs)
    if count >= 3:
        return 1.0
    elif count >= 2:
        return 0.6
    else:
        return 0.3


def _effective_weight(evidence: MemoryEvidence) -> float:
    """计算单条证据的有效权重。"""
    settings = get_settings()
    base = SOURCE_WEIGHTS.get(evidence.source_type, 0.5)
    decay = _time_decay(evidence.observed_at, settings.MEMORY_DECAY_HALF_LIFE_DAYS)
    return base * evidence.weight * decay


def compute_confidence(
    evidence_refs: list[MemoryEvidence],
    contradiction_refs: list[MemoryEvidence] | None = None,
    confirmed_by_user: bool = False,
    independent_task_count: int = 0,
) -> float:
    """计算记忆置信度。

    Args:
        evidence_refs: 支持证据列表
        contradiction_refs: 矛盾证据列表
        confirmed_by_user: 是否已被用户确认
        independent_task_count: 独立任务数

    Returns:
        置信度 [0, 1]
    """
    if not evidence_refs:
        return 0.0

    # 支持分数：1 - Π(1 - positive_effective_weight)
    ind_factor = _independence_factor(evidence_refs)
    support_score = 1.0
    for ev in evidence_refs:
        w = _effective_weight(ev) * ind_factor
        support_score *= (1.0 - w)
    support_score = 1.0 - support_score

    # 冲突分数
    conflict_score = 0.0
    if contradiction_refs:
        conflict_prod = 1.0
        for ev in contradiction_refs:
            w = _effective_weight(ev)
            conflict_prod *= (1.0 - w)
        conflict_score = 1.0 - conflict_prod

    # 用户确认加成
    confirmation_bonus = 0.20 if confirmed_by_user else 0.0

    # 重复任务加成
    settings = get_settings()
    repeated_task_bonus = min(0.15, 0.05 * max(independent_task_count - 1, 0))

    # 最终置信度
    confidence = support_score - 0.60 * conflict_score + confirmation_bonus + repeated_task_bonus
    confidence = max(0.0, min(1.0, confidence))

    logger.debug(
        "confidence computed: support=%.4f conflict=%.4f confirm=%.2f repeat=%.2f -> %.4f",
        support_score, conflict_score, confirmation_bonus, repeated_task_bonus, confidence,
    )

    return round(confidence, 4)


def determine_status(
    confidence: float,
    confirmed_by_user: bool,
    created_by: str = "auto",
) -> MemoryStatus:
    """根据置信度和用户确认状态决定记忆状态。

    Args:
        confidence: 置信度
        confirmed_by_user: 是否被用户确认
        created_by: 创建者 ("auto" | "user")

    Returns:
        目标 MemoryStatus
    """
    settings = get_settings()

    # 用户确认直接 Active
    if confirmed_by_user or created_by == "user":
        return MemoryStatus.ACTIVE

    # 自动学习模式
    if confidence >= settings.MEMORY_ACTIVE_THRESHOLD:
        if settings.MEMORY_AUTO_APPROVAL:
            return MemoryStatus.ACTIVE
        return MemoryStatus.PENDING_APPROVAL

    if confidence >= settings.MEMORY_PENDING_THRESHOLD:
        return MemoryStatus.PENDING_APPROVAL

    return MemoryStatus.CANDIDATE
