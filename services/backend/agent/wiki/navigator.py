"""Wiki Navigator - 有界导航循环。

PR-05 产物：
  - query → page navigation trace
  - 强制 max_pages / max_depth / max_tool_calls / max_tokens / no_revisit
  - Navigator 只回答“下一步读哪一页”，不负责回答用户

流程（文档 15）：
  understand task → resolve entity → open index → choose next link/page → read → 判断是否足够
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent.wiki.contracts import WikiPage
from agent.wiki.navigation_policy import NavigationState, budget_for
from agent.wiki.navigation_tools import NavigationTools
from agent.wiki.store import WikiStore

logger = logging.getLogger("backend.agent.wiki.navigator")

# 各任务首选的页面类型顺序（用于确定性选页）
_PAGE_TYPE_PREFERENCE = {
    "score": ["product", "overview", "capability", "scenario", "limitation"],
    "draft": ["product", "overview", "positioning", "capability", "scenario", "integration"],
    "chat": ["product", "overview", "capability", "scenario", "positioning"],
}


@dataclass
class NavigationOutcome:
    """一次导航的产出。"""

    state: NavigationState
    opened_pages: dict[str, WikiPage] = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)

    @property
    def stop_reason(self) -> str | None:
        return self.state.stop_reason

    @property
    def visited(self) -> list[str]:
        return self.state.visited_pages


class WikiNavigator:
    """有界 Wiki 导航器。结构化、确定性的选页策略，可选 LLM 决策。"""

    def __init__(
        self,
        store: WikiStore,
        tools: NavigationTools | None = None,
        index: Any | None = None,
        llm: Any | None = None,
        preferences: dict[str, list[str]] | None = None,
    ):
        self.store = store
        self.tools = tools or NavigationTools(store, index)
        self.llm = llm
        self.preferences = preferences or _PAGE_TYPE_PREFERENCE

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

        trace: list[dict] = []
        opened: dict[str, WikiPage] = {}

        start_pages = self._start_pages(query, state, trace)
        pending: list[str] = start_pages or []
        seen: set[str] = set()
        tool_calls = 0

        def record(event: str, detail: dict) -> None:
            trace.append({"event": event, **detail})

        while pending and state.can_continue and tool_calls < state.max_tool_calls:
            page_id = pending.pop(0)
            if page_id in seen:
                continue
            if page_id in opened:
                seen.add(page_id)
                continue
            seen.add(page_id)
            tool_calls += 1
            try:
                page = self.store.open_page(page_id)
            except Exception as exc:
                record("wiki.open_page", {"page_id": page_id, "error": str(exc)})
                continue

            state.visit(page_id)
            opened[page_id] = page
            record("wiki.open_page", {"page_id": page_id, "page_type": page.meta.page_type})
            _tokens = self._estimate_tokens(page)
            state.tokens_used += _tokens

            children = self._children_for(page, state.task_type)
            ordered = self._order_by_preference(children, state.task_type)
            for child in ordered:
                if child not in seen and child not in pending:
                    pending.append(child)

        if not state.stop_reason and state.can_continue:
            state.stop_reason = "EXHAUSTED" if pending else "EVIDENCE_COLLECTED"
            record("wiki.stop", {"reason": state.stop_reason})
        else:
            record("wiki.stop", {"reason": state.stop_reason})

        return NavigationOutcome(state=state, opened_pages=opened, trace=trace)

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
        # 3. 兜底：按页码前缀扫描产品
        if not starts:
            for pg in self.store.list_page_ids():
                if pg.split(".", 1)[0] == "product":
                    starts.append(pg)
        trace.append({"event": "wiki.resolve_entity", "starts": starts[:5]})
        return starts

    def _children_for(self, page: WikiPage, task_type: str) -> list[str]:
        links = [r.target_page_id for r in page.meta.relations]
        ordered = []
        for pid in links:
            if page.meta.page_id.split(".", 1)[0] == "product":
                ordered.append(pid)
            else:
                ordered.append(pid)
        return [pid for pid in ordered if self.store.page_exists(pid) and pid != page.meta.page_id]

    def _order_by_preference(self, page_ids: list[str], task_type: str) -> list[str]:
        prefs = self.preferences.get(task_type, self.preferences["score"])
        rank = {pt: i for i, pt in enumerate(prefs)}

        def key(pid: str):
            seg = pid.split(".", 1)[0]
            return (rank.get(seg, 99), pid)

        return sorted(dict.fromkeys(page_ids), key=key)

    @staticmethod
    def _estimate_tokens(page: WikiPage) -> int:
        return len(page.render_markdown()) // 4


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
