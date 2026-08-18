"""Candidate-news presentation and deterministic natural-language selection."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Literal

from agent.business_tools.models import ArticleCandidate
from pydantic import BaseModel, Field


class CandidateSelectionResult(BaseModel):
    outcome: Literal["no_results", "auto_selected", "selected", "needs_selection", "stale"]
    selected: ArticleCandidate | None = None
    candidates: list[ArticleCandidate] = Field(default_factory=list)
    reason: str
    matched_by: str = ""


_CHINESE_ORDINALS = {
    "第一": 1,
    "第一个": 1,
    "首个": 1,
    "第二": 2,
    "第二个": 2,
    "第三": 3,
    "第三个": 3,
    "最后一个": -1,
}

# 用户授权"由 agent 决定"时，直接从当前候选里自动选取首条，不再要求逐条挑选
_AUTO_MARKERS = (
    "你决定",
    "你来定",
    "你看着办",
    "你定",
    "随便",
    "都可以",
    "听你的",
    "由你",
    "自动选择",
    "自动选",
    "全权交给你",
    "auto",
)


class CandidateSelector:
    def select(
        self,
        candidates: list[ArticleCandidate | dict[str, Any]],
        user_text: str = "",
        *,
        auto_threshold: float = 0.85,
        stale_article_ids: set[str] | None = None,
    ) -> CandidateSelectionResult:
        parsed = [
            item if isinstance(item, ArticleCandidate) else ArticleCandidate.model_validate(item)
            for item in candidates
        ]
        stale = set(stale_article_ids or ())
        available = [item for item in parsed if item.article_id not in stale]
        if not available:
            return CandidateSelectionResult(
                outcome="stale" if parsed else "no_results",
                candidates=parsed,
                reason="候选已失效，请重新搜索。" if parsed else "没有找到匹配的新闻。",
            )
        if len(available) == 1 and not user_text.strip():
            candidate = available[0]
            high_confidence = candidate.score is None or candidate.score >= auto_threshold
            if high_confidence and not candidate.duplicate_of:
                return CandidateSelectionResult(
                    outcome="auto_selected",
                    selected=candidate,
                    candidates=available,
                    reason=f"只有一个高置信候选，已选择《{candidate.title}》。",
                    matched_by="single_high_confidence",
                )
        if user_text.strip():
            index = self._ordinal(user_text, len(available))
            if index is not None:
                selected = available[index]
                return CandidateSelectionResult(
                    outcome="selected",
                    selected=selected,
                    candidates=available,
                    reason=f"已按序号选择《{selected.title}》。",
                    matched_by="ordinal",
                )
            selected = self._title_match(user_text, available)
            if selected is not None:
                return CandidateSelectionResult(
                    outcome="selected",
                    selected=selected,
                    candidates=available,
                    reason=f"已按标题描述选择《{selected.title}》。",
                    matched_by="title",
                )
        # 用户授权"由 agent 决定"时自动选首条（最早/最相关候选优先），避免空转等待
        if user_text.strip() and any(marker in user_text for marker in _AUTO_MARKERS):
            selected = available[0]
            return CandidateSelectionResult(
                outcome="auto_selected",
                selected=selected,
                candidates=available,
                reason=f"你已授权由我来定，我选择了《{selected.title}》作为底稿来源。",
                matched_by="user_auto_grant",
            )
        return CandidateSelectionResult(
            outcome="needs_selection",
            candidates=available,
            reason="存在多个相近候选，需要用户选择，不能随机决定。",
        )

    @staticmethod
    def _ordinal(text: str, count: int) -> int | None:
        compact = re.sub(r"\s+", "", text)
        for marker, ordinal in _CHINESE_ORDINALS.items():
            if marker in compact:
                index = count - 1 if ordinal == -1 else ordinal - 1
                return index if 0 <= index < count else None
        match = re.search(r"(?:第|选|要)?\s*(\d{1,2})\s*(?:个|条|篇)?", text)
        if match:
            index = int(match.group(1)) - 1
            return index if 0 <= index < count else None
        return None

    @staticmethod
    def _title_match(text: str, candidates: list[ArticleCandidate]) -> ArticleCandidate | None:
        normalized = re.sub(r"\W+", "", text).lower()
        scored: list[tuple[float, ArticleCandidate]] = []
        for candidate in candidates:
            title = re.sub(r"\W+", "", candidate.title).lower()
            if not title:
                continue
            if title in normalized or (len(normalized) >= 4 and normalized in title):
                score = 1.0
            else:
                score = SequenceMatcher(None, normalized, title).ratio()
            scored.append((score, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored or scored[0][0] < 0.45:
            return None
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.08:
            return None
        return scored[0][1]
