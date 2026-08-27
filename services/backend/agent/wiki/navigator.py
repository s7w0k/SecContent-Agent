"""Wiki Navigator V2 - 有界、requirement-driven 的真图导航（Phase 7 / PR-13）。

PR-07 产物（文档 15 / §10）：
  - NavigationCandidate 携带真实图深度（§10.1）
  - max_depth 检查 candidate.depth 而非访问页数
  - requirement-driven 导航 + Stop Condition（§10.4/§10.7）
  - LLM action 白名单 validate_action（§10.5）
  - Hybrid Policy：确定性 Guard → 决策 → 确定性执行（§10.6）

流程：understand task → resolve entity → open index → choose next link/page
      → read → assess evidence 是否足够。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent.wiki.contracts import WikiPage
from agent.wiki.navigation_policy import NavigationState, budget_for
from agent.wiki.navigation_tools import NavigationTools
from agent.wiki.requirements import RequirementTracker, default_requirements
from agent.wiki.store import WikiStore

logger = logging.getLogger("backend.agent.wiki.navigator")

# 各任务首选的页面类型顺序（用于确定性选页）
_PAGE_TYPE_PREFERENCE = {
    "score": ["product", "overview", "capability", "scenario", "limitation"],
    "draft": ["product", "overview", "positioning", "capability", "scenario", "integration"],
    "chat": ["product", "overview", "capability", "scenario", "positioning"],
}

# LLM 允许的动作白名单（§10.3）
ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        "RESOLVE_ENTITY",
        "SEARCH_PAGES",
        "OPEN_PAGE",
        "OPEN_SECTION",
        "LIST_LINKS",
        "FOLLOW_LINK",
        "READ_SOURCE",
        "ASSESS_EVIDENCE",
        "STOP_SUFFICIENT",
        "STOP_INSUFFICIENT",
    }
)

# requirement 覆盖达标线（§10.7）
STOP_COVERAGE_THRESHOLD = 0.7


@dataclass
class NavigationCandidate:
    """导航候选，携带真实图深度（§10.1）。"""

    page_id: str
    depth: int
    parent_page_id: str | None = None
    via_relation: str = ""
    score: float = 0.0


@dataclass
class NavigationOutcome:
    """一次导航的产出。"""

    state: NavigationState
    opened_pages: dict[str, WikiPage] = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)
    satisfaction: float = 0.0

    @property
    def stop_reason(self) -> str | None:
        return self.state.stop_reason

    @property
    def visited(self) -> list[str]:
        return self.state.visited_pages


class WikiNavigator:
    """有界 Wiki 导航器。确定性引擎 + 可选 LLM 决策（受白名单约束）。"""

    def __init__(
        self,
        store: WikiStore,
        tools: NavigationTools | None = None,
        index: Any | None = None,
        llm: Any | None = None,
        preferences: dict[str, list[str]] | None = None,
        resolver: Any | None = None,
    ):
        self.store = store
        self.tools = tools or NavigationTools(store, index)
        self.llm = llm
        self.preferences = preferences or _PAGE_TYPE_PREFERENCE
        self.resolver = resolver

    def state_for(self, task_type: str, query: str, product_ids: list[str]) -> NavigationState:
        budget = budget_for(task_type)
        return NavigationState(
            task_type=task_type,
            query=query,
            product_ids=product_ids,
            max_pages=budget.max_pages,
            max_depth=budget.max_depth,
            max_tool_calls=budget.max_tool_calls,
            token_budget=budget.max_tokens,
        )

    async def navigate(
        self,
        query: str,
        product_ids: list[str] | None = None,
        task_type: str = "score",
        max_pages: int | None = None,
        max_depth: int | None = None,
    ) -> NavigationOutcome:
        state = self.state_for(task_type, query, product_ids or [])
        if max_pages is not None:
            state.max_pages = max_pages
        if max_depth is not None:
            state.max_depth = max_depth

        requirements = default_requirements(task_type)
        tracker = RequirementTracker(requirements)
        trace: list[dict] = []
        opened: dict[str, WikiPage] = {}

        def record(event: str, detail: dict) -> None:
            trace.append({"event": event, **detail})

        start_ids = self._start_pages(query, state, trace)
        frontier: list[NavigationCandidate] = [
            NavigationCandidate(page_id=pid, depth=0) for pid in start_ids or []
        ]
        state.frontier = [c.page_id for c in frontier]
        seen: set[str] = set()
        tool_calls = 0

        while frontier and state.can_continue and tool_calls < state.max_tool_calls:
            candidate = frontier.pop(0)
            page_id = candidate.page_id
            if page_id in seen:
                continue
            seen.add(page_id)

            # max_depth 检查真实深度（§10.1）
            if candidate.depth > state.max_depth:
                continue

            tool_calls += 1
            try:
                page = self.store.open_page(page_id)
            except Exception as exc:
                record("wiki.open_page", {"page_id": page_id, "error": str(exc)})
                continue

            state.visit(page_id, depth=candidate.depth)
            opened[page_id] = page
            tracker.observe_page(page)
            record("wiki.open_page", {"page_id": page_id, "page_type": page.meta.page_type})
            state.tokens_used += self._estimate_tokens(page)

            children = self._children_for(page, candidate, seen)
            ordered = self._order_by_preference(children, state.task_type)
            pending_ids = {c.page_id for c in frontier}
            for child in ordered:
                if child.page_id in seen or child.page_id in pending_ids:
                    continue
                frontier.append(child)
                state.frontier.append(child.page_id)

        state.missing_requirements = tracker.missing
        state.evidence_so_far = sum(1 for _r in requirements if _r.requirement_id in tracker.met)
        satisfaction = tracker.coverage()

        # Stop Condition（§10.7）
        if state.stop_reason is None:
            if satisfaction >= STOP_COVERAGE_THRESHOLD:
                state.mark_stop("SUFFICIENT")
            elif not state.can_continue:
                state.mark_stop("BUDGET_EXHAUSTED")
            else:
                state.mark_stop("EVIDENCE_COLLECTED")
        record("wiki.stop", {"reason": state.stop_reason, "coverage": round(satisfaction, 3)})
        return NavigationOutcome(
            state=state, opened_pages=opened, trace=trace, satisfaction=satisfaction
        )

    # ── 起点页面 ──────────────────────────────────────────

    def _start_pages(self, query: str, state: NavigationState, trace: list[dict]) -> list[str]:
        starts: list[str] = []
        # 1. 指定产品 → 打开产品索引
        for pid in state.product_ids:
            product_page = "product." + pid
            if self.store.page_exists(product_page):
                starts.append(product_page)
        # 2. 实体解析
        for name in _extract_entity_candidates(query):
            for res in self.tools.resolve_entity(name):
                pid = res.get("page_id")
                if pid and pid not in starts:
                    starts.append(pid)
        # 3. 有界 descriptor 搜索 + 消歧（§9.3）
        if not starts:
            result = self.resolver.resolve(query) if self.resolver else None
            if result is not None and result.candidates:
                resolved = [
                    c for c in result.candidates if c.score >= self.resolver.resolve_threshold
                ][:5]
                if resolved:
                    starts = [c.page_id for c in resolved]
                else:
                    state.mark_stop("AMBIGUOUS_ENTITY")
                    trace.append(
                        {
                            "event": "wiki.resolve_entity",
                            "status": "AMBIGUOUS_ENTITY",
                            "candidates": [
                                {"page_id": c.page_id, "score": c.score} for c in result.candidates
                            ],
                        }
                    )
                    return starts
            else:
                state.mark_stop("UNKNOWN_ENTITY")
                trace.append({"event": "wiki.resolve_entity", "status": "UNKNOWN_ENTITY"})
                return starts
        trace.append({"event": "wiki.resolve_entity", "starts": starts[:5]})
        return starts

    def _children_for(
        self, page: WikiPage, parent: NavigationCandidate, seen: set[str]
    ) -> list[NavigationCandidate]:
        children: list[NavigationCandidate] = []
        for rel in page.meta.relations:
            target = rel.target_page_id
            # self-link / broken target / 已访问 直接跳过
            if target == page.meta.page_id or target in seen or not self.store.page_exists(target):
                continue
            children.append(
                NavigationCandidate(
                    page_id=target,
                    depth=parent.depth + 1,
                    parent_page_id=page.meta.page_id,
                    via_relation=rel.relation_type,
                )
            )
        return children

    def _order_by_preference(
        self, candidates: list[NavigationCandidate], task_type: str
    ) -> list[NavigationCandidate]:
        prefs = self.preferences.get(task_type, self.preferences["score"])
        rank = {pt: i for i, pt in enumerate(prefs)}

        def key(c: NavigationCandidate):
            seg = c.page_id.split(".", 1)[0]
            return (rank.get(seg, 99), c.page_id)

        # 同 page_id 去重（保留首个）
        seen_ids: set[str] = set()
        unique: list[NavigationCandidate] = []
        for c in sorted(candidates, key=key):
            if c.page_id in seen_ids:
                continue
            seen_ids.add(c.page_id)
            unique.append(c)
        return unique

    # ── LLM Action 白名单校验（§10.5）──────────────────────

    def validate_action(
        self,
        action: dict,
        *,
        state: NavigationState,
        frontier: list[NavigationCandidate],
        visited: set[str],
    ) -> tuple[bool, str]:
        """校验 LLM 提议的 action。非法 → (False, reason)，调用方计数并走 deterministic fallback。"""
        kind = action.get("action", "")
        if kind not in ALLOWED_ACTIONS:
            return False, f"ACTION_NOT_ALLOWED:{kind or 'unknown'}"

        target = action.get("target", "")
        # STOP 类动作无需 target
        if kind in {"STOP_SUFFICIENT", "STOP_INSUFFICIENT"}:
            return True, ""

        if not target or (state.product_ids and _tenant_mismatch(target, state.product_ids)):
            return False, "TARGET_NOT_IN_PRODUCTS"

        # OPEN_PAGE/FOLLOW_LINK 需要 target 在候选列表
        if kind in {"OPEN_PAGE", "FOLLOW_LINK", "OPEN_SECTION"}:
            ids = {c.page_id for c in frontier}
            if target not in ids:
                # READ 型动作允许打开 visited 中已解析目标，但不允许发明 page_id
                if target in visited:
                    return True, ""
                return False, "TARGET_NOT_IN_CANDIDATES"

        # budget 合法
        if len(state.visited_pages) >= state.max_pages or state.depth >= state.max_depth:
            return False, "BUDGET_INVALID"
        return True, ""

    @staticmethod
    def _estimate_tokens(page: WikiPage) -> int:
        return len(page.render_markdown()) // 4


def _tenant_mismatch(target: str, product_ids: list[str]) -> bool:
    """粗粒度租户/产品边界：target 须归属某个显式请求的产品。"""
    for pid in product_ids:
        if target.startswith("product.") and target.startswith("product." + pid):
            return False
    return True


def _extract_entity_candidates(query: str) -> list[str]:
    """从 query 中提取实体候选名。"""
    stopwords = {"的", "产品", "是否", "能", "可以", "为什么", "与", "相关", "一个"}
    tokens = [t for t in query.replace("，", " ").replace("？", " ").split() if t]
    results: list[str] = []
    for t in tokens:
        if t not in stopwords and 1 < len(t) <= 12:
            results.append(t)
    # 长查询补全一个整体候选
    if len(query) <= 12 and result_is_single(query):
        results.append(query)
    return results


def result_is_single(t: str) -> bool:
    return 1 < len(t) <= 12
