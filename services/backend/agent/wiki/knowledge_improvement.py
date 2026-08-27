"""安全 Self-Evolution（Phase 25 / PR-22）— 运行时只产生事件，不直接写 Wiki。

原则：Runtime 不能直接写 Wiki，只产生 `KnowledgeImprovementEvent`，
经 Deduplicate → Maintainer Proposal → Staging Compile → Lint/Gate → Publish。

关键安全规则（计划 §28）：**User statement ≠ Trusted Source**
  - 来自用户输入的观察永远打上 `trusted=False`
  - 只有 `trusted=True`（来自已验证 Raw Source）的事件才允许被推荐为 Wiki 事实
  - 不可信事件仅进入 Proposal，绝不直接写入 PCRoduction Wiki Fact，防止知识投毒。

纯标准库 + pydantic，无第三方新增依赖。
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

# 事件类型（计划 §28）
IMPROVEMENT_TYPES = frozenset(
    {
        "MISSING_PAGE",
        "MISSING_ALIAS",
        "BROKEN_LINK_OBSERVED",
        "REPEATED_NAVIGATION_PATH",
        "LOW_COVERAGE_TOPIC",
        "POTENTIAL_SYNTHESIS",
        "STALE_KNOWLEDGE",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class KnowledgeImprovementEvent(BaseModel):
    """一条运行时知识改进事件。"""

    event_id: str = Field(description="稳定事件 ID")
    type: str = Field(description="事件类型，见 IMPROVEMENT_TYPES")
    subject: str = Field(description="事件主体：产品/实体/alias/page_id")
    detail: str = Field(default="", description="观察详情")
    trusted: bool = Field(description="是否来自已验证 Raw Source（用户输入= False）")
    source_hint: str = Field(default="", description="来源提示（如 source_id / 用户语句）")
    created_at: str = Field(default_factory=_now)


def _dedup_key(event: KnowledgeImprovementEvent) -> str:
    """规范化去重键：区分大小写、归一化空白。"""
    norm = re.sub(r"\s+", " ", event.detail or "").strip().lower()
    return f"{event.type}:{event.subject}:{norm}"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


class ImprovementJournal:
    """去重的改进事件日志。未达到 'Maintainer Proposal → Publish' 前不触碰 Wiki。"""

    def __init__(self) -> None:
        self._events: dict[str, KnowledgeImprovementEvent] = {}
        self._order: list[str] = []

    def peek_key(self, event: KnowledgeImprovementEvent) -> str:
        return _dedup_key(event)

    def record(self, event: KnowledgeImprovementEvent) -> bool:
        """记录事件；若与既有事件重复返回 False，否则存入并返回 True。"""
        key = _dedup_key(event)
        if key in self._events:
            return False
        self._events[key] = event
        self._order.append(key)
        return True

    def is_duplicate(self, event: KnowledgeImprovementEvent) -> bool:
        return _dedup_key(event) in self._events

    def count(self) -> int:
        return len(self._events)

    def all(self) -> list[KnowledgeImprovementEvent]:
        return [self._events[k] for k in self._order]


def emit_event(
    *,
    event_type: str,
    subject: str,
    detail: str = "",
    trusted: bool,
    source_hint: str = "",
    journal: ImprovementJournal | None = None,
) -> KnowledgeImprovementEvent | None:
    """构造并记录一条改进事件（线程外调用方负责并发串行化）。

    返回 None 表示重复事件被去重丢弃。
    """
    if event_type not in IMPROVEMENT_TYPES:
        raise ValueError(f"未知改进事件类型: {event_type!r}")
    event = KnowledgeImprovementEvent(
        event_id="",  # 用去重键哈希生成稳定 ID
        type=event_type,
        subject=subject,
        detail=detail,
        trusted=trusted,
        source_hint=source_hint,
    )
    event.event_id = _hash_key(_dedup_key(event))
    if journal is None:
        journal = ImprovementJournal()
    return event if journal.record(event) else None


def to_maintainer_proposal(
    event: KnowledgeImprovementEvent, *, permit_untrusted: bool = False
) -> dict[str, Any] | None:
    """把事件转成 'Maintainer Proposal'。

    安全门禁：只有 `trusted=True`（或显式 `permit_untrusted=True` 供人工维护）的
    事件才有资格被推荐为 Wiki 事实。用户输入（untrusted）默认不允许进入
    Production 事实链，只作为 Proposal 供人工/可信来源核验（防知识投毒）。
    """
    if not event.trusted and not permit_untrusted:
        return None
    return {
        "event_id": event.event_id,
        "type": event.type,
        "subject": event.subject,
        "detail": event.detail,
        "source_hint": event.source_hint,
        "trusted": event.trusted,
        "stage": "proposal_for_maintainer",
    }
