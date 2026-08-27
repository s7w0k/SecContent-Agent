"""Navigation Policy - 各任务类型的导航预算与状态机。

PR-05 产物：
  - 每个任务类型的 max_pages / max_depth / max_tool_calls / max_tokens 预算
  - NavigationState：一次导航的有界状态（禁止无限循环）
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

# 文档 15 推荐的默认预算
DEFAULT_POLICIES: dict[str, dict[str, int]] = {
    "score": {"max_pages": 6, "max_depth": 3, "max_tool_calls": 10, "max_tokens": 12000},
    "draft": {"max_pages": 8, "max_depth": 4, "max_tool_calls": 14, "max_tokens": 16000},
    "chat": {"max_pages": 8, "max_depth": 4, "max_tool_calls": 14, "max_tokens": 16000},
}


@dataclass(frozen=True)
class NavigationBudget:
    """一次导航的资源预算。"""

    max_pages: int
    max_depth: int
    max_tool_calls: int
    max_tokens: int


def budget_for(task_type: str) -> NavigationBudget:
    """取某任务类型的导航预算；未知返回 score 默认。"""
    conf = DEFAULT_POLICIES.get(task_type, DEFAULT_POLICIES["score"])
    return NavigationBudget(**conf)


class NavigationState(BaseModel):
    """一次导航的有界状态。"""

    task_type: str = "score"
    query: str = ""
    product_ids: list[str] = Field(default_factory=list)

    current_page: str | None = Field(default=None)
    visited_pages: list[str] = Field(default_factory=list)
    candidate_pages: list[str] = Field(default_factory=list)

    # ── Navigator V2（§10.2）───────────────────────────────
    resolved_entities: list[str] = Field(default_factory=list)
    frontier: list[str] = Field(default_factory=list)
    visited_sections: list[str] = Field(default_factory=list)
    evidence_so_far: int = Field(default=0)
    missing_requirements: list[str] = Field(default_factory=list)
    repeated_actions: int = Field(default=0)

    # ── PR-A：LLM 决策防循环计数器（§4.8）──────────────────
    action_history: list[str] = Field(default_factory=list)
    invalid_action_count: int = Field(default=0)
    repeated_action_count: int = Field(default=0)
    llm_failure_count: int = Field(default=0)

    evidence_count: int = Field(default=0)

    max_pages: int = Field(default=6)
    max_depth: int = Field(default=3)
    max_tool_calls: int = Field(default=10)
    token_budget: int = Field(default=12000)
    tokens_used: int = Field(default=0)

    depth: int = Field(default=0)
    stop_reason: str | None = Field(default=None)

    @property
    def can_continue(self) -> bool:
        if self.stop_reason is not None:
            return False
        if len(self.visited_pages) >= self.max_pages:
            self.stop_reason = "MAX_PAGES"
            return False
        if self.depth >= self.max_depth:
            self.stop_reason = "MAX_DEPTH"
            return False
        if self.tokens_used >= self.token_budget:
            self.stop_reason = "MAX_TOKENS"
            return False
        return True

    def visit(self, page_id: str, depth: int | None = None) -> None:
        if page_id not in self.visited_pages:
            self.visited_pages.append(page_id)
        # 真实图深度（§10.1）：由候选携带的 depth 决定，而非访问页数累积
        if depth is None:
            depth = min(self.depth + 1, self.max_depth)
        self.depth = min(depth, self.max_depth)
        if len(self.visited_pages) >= self.max_pages:
            self.stop_reason = "MAX_PAGES"

    def mark_stop(self, reason: str) -> None:
        self.stop_reason = reason
