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
from agent.wiki.navigation_decider import NavigationAction, NavigationDecisionContext
from agent.wiki.navigation_policy import NavigationState, budget_for
from agent.wiki.navigation_tools import NavigationTools
from agent.wiki.requirements import affinity_for_page_type, default_requirements
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
    # GOAL A/PR-3：导航期间的单一事实源——已验证 Evidence Snapshot。
    # Provider 直接复用该 Snapshot 组装最终 Bundle，禁止导航后再算第二套 Coverage。
    evidence_snapshot: Any = None

    @property
    def stop_reason(self) -> str | None:
        return self.state.stop_reason

    @property
    def visited(self) -> list[str]:
        return self.state.visited_pages


class WikiNavigator:
    """有界 Wiki 导航器。确定性引擎 + 可选 LLM 决策（受白名单 Harness 约束）。"""

    def __init__(
        self,
        store: WikiStore,
        tools: NavigationTools | None = None,
        index: Any | None = None,
        llm: Any | None = None,
        decider: Any | None = None,
        preferences: dict[str, list[str]] | None = None,
        resolver: Any | None = None,
        *,
        max_invalid_actions: int = 2,
        max_llm_failures: int = 2,
        no_progress_stop_rounds: int = 2,
    ):
        self.store = store
        self.tools = tools or NavigationTools(store, index)
        self.llm = llm
        self.preferences = preferences or _PAGE_TYPE_PREFERENCE
        self.resolver = resolver
        # PR-A：LLM 决策器；llm 非空但未显式给 decider 时按需构造
        self._decider = decider
        self.max_invalid_actions = max_invalid_actions
        self.max_llm_failures = max_llm_failures
        # GOAL A/§14：仍有候选但连续无进展的轮次上限（达到后才允许 raise STOP_INSUFFICIENT）
        self.no_progress_stop_rounds = max(1, no_progress_stop_rounds)

    @property
    def llm_enabled(self) -> bool:
        return self.llm is not None

    def _decider_for(self, llm: Any):
        if self._decider is not None:
            return self._decider
        from agent.wiki.navigation_decider import LLMNavigationDecider

        return LLMNavigationDecider(llm=llm)

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
        evidence_session: Any | None = None,
    ) -> NavigationOutcome:
        """GOAL A（§11）：Live Evidence-driven Navigation。

        - 每打开一页立即 `assess_page`（增量 Collect→Verify→Evaluate），更新 live snapshot
        - Requirement MET / missing / coverage / confidence 全部来自 **Verified Evidence Snapshot**
        - `sufficient` 由快照硬校验；LLM 的 STOP 需通过 `validate_action` 校验
        - `evidence_session` 由 `WikiKnowledgeProvider` 按请求创建并注入
        """
        state = self.state_for(task_type, query, product_ids or [])
        if max_pages is not None:
            state.max_pages = max_pages
        if max_depth is not None:
            state.max_depth = max_depth

        requirements = default_requirements(task_type)
        snapshot_holder: dict[str, Any] = {"current": None}
        if evidence_session is not None:
            snapshot_holder["current"] = evidence_session.initial_snapshot()

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
        llm_disabled = False  # 本请求内 LLM 决策被禁用（反复失败/非法）

        decider = self._decider_for(self.llm) if self.llm_enabled else None

        # GOAL A/§14：no-progress 检测
        prev_evidence = 0
        prev_coverage = 0.0
        prev_missing: set[str] = set()

        while frontier and state.can_continue and tool_calls < state.max_tool_calls:
            snapshot = snapshot_holder["current"]
            missing = list(snapshot.missing_requirements) if snapshot else []
            candidates = self._build_descriptors(frontier, requirements)

            # ── 决策：LLM（受 Harness 约束）或确定性兜底 ──
            action, used_llm = await self._choose_action(
                state=state,
                candidates=candidates,
                frontier=frontier,
                seen=seen,
                requirements=requirements,
                snapshot=snapshot,
                missing_requirements=missing,
                task_type=task_type,
                query=query,
                llm_enabled=self.llm_enabled and not llm_disabled,
                decider=decider,
                trace=trace,
                record=record,
            )
            if action is None:
                break

            if used_llm and action.action.startswith("STOP"):
                record("wiki.llm_decision", {"status": "STOP_REJECTED"})

            # ── 执行（Harness：任何越权/预算都用拓扑兜底）──
            executed = self._execute_action(
                action=action,
                state=state,
                frontier=frontier,
                seen=seen,
                opened=opened,
                evidence_session=evidence_session,
                snapshot_holder=snapshot_holder,
                tool_calls=tool_calls,
                record=record,
            )
            if executed:
                tool_calls += 1

            # ── GOAL A：用快照刷新导航状态 + no-progress 判定 ──
            snapshot = snapshot_holder["current"]
            if snapshot is not None:
                state.missing_requirements = list(snapshot.missing_requirements)
                state.coverage = snapshot.coverage
                state.confidence = snapshot.confidence
                state.evidence_so_far = len(snapshot.evidence)
                cur_missing: set[str] = set(snapshot.missing_requirements)
                no_progress = (
                    len(snapshot.evidence) <= prev_evidence
                    and snapshot.coverage <= prev_coverage + 1e-9
                    and prev_missing.issubset(cur_missing)
                )
                state.no_progress_rounds = (
                    state.no_progress_rounds + 1 if no_progress else 0
                )
                prev_evidence = len(snapshot.evidence)
                prev_coverage = snapshot.coverage
                prev_missing = cur_missing
                # STOP_SUFFICIENT 必须由 Verified Evidence 状态硬校验（§13）
                if snapshot.sufficient:
                    state.mark_stop("SUFFICIENT")
                    break

        snapshot = snapshot_holder["current"]
        if snapshot is not None:
            state.missing_requirements = list(snapshot.missing_requirements)
            state.coverage = snapshot.coverage
            state.confidence = snapshot.confidence
            state.evidence_so_far = len(snapshot.evidence)
            satisfaction = snapshot.coverage
        else:
            # 无注入 session（测试/工具直接调用）：仅作导航 hint，不判定 Requirement MET
            hint_missing, hint_met = _navigator_hints(state.visited_pages, task_type)
            state.missing_requirements = hint_missing
            state.evidence_so_far = hint_met
            satisfaction = 0.0

        # Stop Condition（导航层仅作导航终止；SUFFICIENT 已由上面 Verified 快照判定）
        if state.stop_reason is None:
            if not state.can_continue:
                state.mark_stop("BUDGET_EXHAUSTED")
            else:
                state.mark_stop("EVIDENCE_COLLECTED")
        record("wiki.stop", {"reason": state.stop_reason, "coverage": round(satisfaction, 3)})
        return NavigationOutcome(
            state=state,
            opened_pages=opened,
            trace=trace,
            satisfaction=satisfaction,
            evidence_snapshot=snapshot,
        )

    async def _choose_action(
        self,
        *,
        state: NavigationState,
        candidates: list[dict],
        frontier: list[NavigationCandidate],
        seen: set[str],
        requirements: list[Any],
        snapshot: Any | None,
        missing_requirements: list[str],
        task_type: str,
        query: str,
        llm_enabled: bool,
        decider: Any | None,
        trace: list[dict],
        record,  # callable
    ) -> tuple[NavigationAction | None, bool]:
        """返回 (action, used_llm)。LLM 决策非法/失败则回退 deterministic（used_llm=False）。"""
        if llm_enabled and decider is not None:
            context_kwargs = {
                "query": query,
                "task_type": task_type,
                "requirements": [r.model_dump() for r in requirements],
                "missing_requirements": missing_requirements,
                "visited_pages": list(state.visited_pages),
                "candidate_pages": candidates,
                "pages_remaining": max(0, state.max_pages - len(state.visited_pages)),
                "tool_calls_remaining": max(0, state.max_tool_calls - len(state.action_history)),
                "tokens_remaining": max(0, state.token_budget - state.tokens_used),
                "coverage": snapshot.coverage if snapshot else 0.0,
                "confidence": snapshot.confidence if snapshot else 0.0,
                "verified_evidence_count": (
                    len([e for e in snapshot.evidence if e.reason_code == "VERIFIED"])
                    if snapshot
                    else 0
                ),
                "conflicted_requirements": (
                    [r.requirement_id for r in snapshot.requirements if r.status == "CONFLICTED"]
                    if snapshot
                    else []
                ),
            }

            try:
                state.missing_requirements = missing_requirements  # 刷新，供 validate_action STOP 守卫
                action = await decider.decide(NavigationDecisionContext(**context_kwargs))
            except Exception:
                state.llm_failure_count += 1
                if state.llm_failure_count >= self.max_llm_failures:
                    llm_enabled = False
                record("wiki.llm_decision", {"status": "FAILED", "count": state.llm_failure_count})
                return self._deterministic_action(frontier, state), False

            ok, reason = self.validate_action(
                action.model_dump(),
                state=state,
                frontier=frontier,
                visited=seen,
                snapshot=snapshot,
                missing_requirements=missing_requirements,
            )
            if not ok:
                state.invalid_action_count += 1
                if state.invalid_action_count >= self.max_invalid_actions:
                    llm_enabled = False
                record(
                    "wiki.llm_decision",
                    {"status": "INVALID", "action": action.action, "reason": reason},
                )
                return self._deterministic_action(frontier, state), False

            if self._is_repeated(state, action):
                state.repeated_action_count += 1
                record(
                    "wiki.llm_decision",
                    {"status": "REPEATED", "action": action.action, "target": action.target},
                )
                return self._deterministic_action(frontier, state), False

            state.action_history.append(f"{action.action}:{action.target}")
            record(
                "wiki.llm_decision",
                {
                    "status": "ACCEPTED",
                    "action": action.action,
                    "target": action.target,
                    "requirement_id": action.requirement_id,
                },
            )
            return action, True

        return self._deterministic_action(frontier, state), False

    def _deterministic_action(self, frontier: list[NavigationCandidate], state: NavigationState):
        from agent.wiki.navigation_decider import deterministic_decision

        candidates = [
            {
                "page_id": c.page_id,
                "via_relation": c.via_relation,
                "depth": c.depth,
                "task_affinity": _requirement_affinity(c.page_id),
            }
            for c in frontier
        ]
        return deterministic_decision(candidates, missing_requirements=list(state.missing_requirements))

    def _is_repeated(self, state: NavigationState, action) -> bool:
        key = f"{action.action}:{action.target}"
        history = state.action_history[-2:]
        return len(history) >= 2 and history[-1] == key and history[-2] == key

    def _execute_action(
        self,
        *,
        action,
        state: NavigationState,
        frontier: list[NavigationCandidate],
        seen: set[str],
        opened: dict[str, WikiPage],
        evidence_session: Any | None,
        snapshot_holder: dict[str, Any] | None,
        tool_calls: int,
        record,
    ) -> bool:
        """执行 action；返回是否消耗了一个 tool_call。越权一律忽略（Harness）。

        GOAL A：每打开一个新 Wiki Page 立即 `assess_page`（增量 Collect→Verify→Evaluate），
        把当前 Verified 快照写回 `snapshot_holder`。不再 `tracker.observe_page(page)`。
        """
        if action.action in {"STOP_SUFFICIENT", "STOP_INSUFFICIENT"}:
            if action.action == "STOP_SUFFICIENT":
                state.mark_stop("SUFFICIENT")
            else:
                state.mark_stop("EVIDENCE_COLLECTED")
            return False

        # 其余动作都要落到某个 Candidate 页面
        target = action.target or ""
        idx = next((i for i, c in enumerate(frontier) if c.page_id == target), None)
        if idx is None:
            return False  # target 不在候选，忽略（validate_action 已拦住，二次防御）
        candidate = frontier.pop(idx)
        state.frontier = [c.page_id for c in frontier]
        if candidate.page_id in seen:
            return False

        # max_depth 检查真实深度（§10.1）
        if candidate.depth > state.max_depth:
            return False

        try:
            page = self.store.open_page(candidate.page_id)
        except Exception as exc:
            record("wiki.open_page", {"page_id": candidate.page_id, "error": str(exc)})
            return False

        seen.add(candidate.page_id)
        state.visit(candidate.page_id, depth=candidate.depth)
        opened[candidate.page_id] = page
        # GOAL A：开页后立即增量评估，更新 Live Evidence Snapshot
        if evidence_session is not None and snapshot_holder is not None:
            snapshot_holder["current"] = evidence_session.assess_page(
                page_id=candidate.page_id,
                page=page,
            )
        record(
            "wiki.open_page",
            {"page_id": candidate.page_id, "page_type": page.meta.page_type},
        )
        state.tokens_used += self._estimate_tokens(page)

        children = self._children_for(page, candidate, seen)
        ordered = self._order_by_preference(children, state.task_type)
        pending_ids = {c.page_id for c in frontier}
        for child in ordered:
            if child.page_id in seen or child.page_id in pending_ids:
                continue
            frontier.append(child)
            state.frontier.append(child.page_id)
        return True

    def _build_descriptors(
        self, frontier: list[NavigationCandidate], requirements
    ) -> list[dict]:
        """把 frontier 压成 ≤8 个 PageDescriptor（§4.3）。"""
        page_type_affinity: dict[str, list[str]] = {}
        for r in requirements:
            for pt in r.required_page_types:
                page_type_affinity.setdefault(pt, []).append(r.requirement_id)

        descs: list[dict] = []
        for c in frontier[:8]:
            page_id = c.page_id
            page_type = page_id.split(".", 1)[0] if "." in page_id else page_id
            title = ""
            summary = ""
            try:
                page = self.store.open_page(page_id)
                page_type = page.meta.page_type or page_type
                title = page.meta.title
                summary = (page.summary() or "")[:300]
            except Exception:
                pass
            descs.append(
                {
                    "page_id": page_id,
                    "title": title,
                    "page_type": page_type,
                    "summary": summary,
                    "via_relation": c.via_relation,
                    "depth": c.depth,
                    "task_affinity": list(page_type_affinity.get(page_type, [])),
                }
            )
        return descs

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
        snapshot: Any | None = None,
        missing_requirements: list[str] | None = None,
    ) -> tuple[bool, str]:
        """校验 LLM 提议的 action。非法 → (False, reason)，调用方计数并走 deterministic fallback。

        GOAL A（§13/§14）：
          - STOP_SUFFICIENT 必须由 Verified Evidence Snapshot 硬校验（.sufficient）
          - STOP_INSUFFICIENT 若仍有能补齐 Missing Requirement 的合法 Candidate 且无足够
            无进展轮次，则拒绝过早停止
        """
        kind = action.get("action", "")
        if kind not in ALLOWED_ACTIONS:
            return False, f"ACTION_NOT_ALLOWED:{kind or 'unknown'}"

        missing = (
            list(missing_requirements)
            if missing_requirements is not None
            else list(state.missing_requirements)
        )
        target = action.get("target", "")
        # STOP 类动作基于 Verified Evidence 状态（§13/§14）
        if kind == "STOP_SUFFICIENT":
            sufficient = bool(snapshot is not None and snapshot.sufficient)
            if not sufficient:
                return False, "EVIDENCE_NOT_SUFFICIENT"
            return True, ""
        if kind == "STOP_INSUFFICIENT":
            actionable = bool(snapshot is not None) and self._has_actionable_candidate(
                frontier, missing, state
            )
            if actionable and state.no_progress_rounds < self.no_progress_stop_rounds:
                return False, "STOP_INSUFFICIENT_PREMATURE"
            return True, ""

        if not target or (state.product_ids and _tenant_mismatch(target, state.product_ids)):
            return False, "TARGET_NOT_IN_PRODUCTS"

        # OPEN_PAGE/FOLLOW_LINK/OPEN_SECTION 需要 target 在候选列表且未被访问
        # （LLM 不能发明 page_id、不能重开已访问页面，§4.2/§4.9）
        if kind in {"OPEN_PAGE", "FOLLOW_LINK", "OPEN_SECTION"}:
            ids = {c.page_id for c in frontier}
            if target in visited:
                return False, "TARGET_ALREADY_VISITED"
            if target not in ids:
                return False, "TARGET_NOT_IN_CANDIDATES"

        # budget 合法
        if len(state.visited_pages) >= state.max_pages or state.depth >= state.max_depth:
            return False, "BUDGET_INVALID"
        return True, ""

    def _has_actionable_candidate(
        self,
        frontier: list[NavigationCandidate],
        missing_requirements: list[str],
        state: NavigationState,
    ) -> bool:
        """是否存在能补齐 Missing Requirement 的合法 Candidate（§14 防过早 STOP_INSUFFICIENT）。"""
        if not missing_requirements:
            return False
        if len(state.visited_pages) >= state.max_pages or state.depth >= state.max_depth:
            return False
        for c in frontier:
            affine = set(_requirement_affinity(c.page_id))
            if affine & set(missing_requirements):
                return True
        return False

    @staticmethod
    def _estimate_tokens(page: WikiPage) -> int:
        return len(page.render_markdown()) // 4


def _tenant_mismatch(target: str, product_ids: list[str]) -> bool:
    """粗粒度租户/产品边界：target 须归属某个显式请求的产品。"""
    for pid in product_ids:
        if target.startswith("product.") and target.startswith("product." + pid):
            return False
    return True


def _requirement_affinity(page_id: str) -> list[str]:
    """为确定性决策粗估页面可能满足的 Requirement（按页面类型前缀）。"""
    seg = page_id.split(".", 1)[0] if "." in page_id else page_id
    mapping = {"capability": ["R1"], "scenario": ["R2"], "limitation": ["R3"]}
    return list(mapping.get(seg, []))


def _navigator_hints(visited: list[str], task_type: str) -> tuple[list[str], int]:
    """无注入 session 时的纯导航 hint（GOAL A：只作导航提示，不判定 Requirement MET）。

    返回 (missing_requirements, met_hint_count)。仅用于直接调用 Navigator 的场景，
    生产路径始终注入 `NavigationEvidenceSession`，Requirement 状态以 Verified Snapshot 为准。
    """
    satisfied: set[str] = set()
    for pid in visited:
        seg = pid.split(".", 1)[0] if "." in pid else pid
        satisfied.update(affinity_for_page_type(task_type, seg))
    reqs = default_requirements(task_type)
    missing = [r.requirement_id for r in reqs if r.requirement_id not in satisfied]
    return missing, len(satisfied)


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
