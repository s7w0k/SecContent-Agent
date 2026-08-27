"""Phase 7 / PR-13：Requirement Planner + Navigator V2（G-05/G-06，§10）。"""

from __future__ import annotations

from agent.wiki.contracts import WikiRelation
from agent.wiki.navigation_policy import NavigationState
from agent.wiki.navigator import ALLOWED_ACTIONS, NavigationCandidate, WikiNavigator
from agent.wiki.requirements import RequirementTracker, default_requirements
from helpers import make_page


async def _single_product_wiki(store):
    store.write_page(
        make_page(
            "product.agent_identity",
            product_id="agent_identity",
            relations=[
                WikiRelation(
                    relation_type="related_to",
                    target_page_id="product.agent_identity.capability.identity_auth",
                )
            ],
        )
    )
    store.write_page(
        make_page(
            "product.agent_identity.capability.identity_auth",
            product_id="agent_identity",
        )
    )


# ── Requirement Planner（§10.4）───────────────────────────


def test_default_requirements_score():
    reqs = default_requirements("score")
    assert [r.requirement_id for r in reqs] == ["R1", "R2", "R3"]
    assert all(0.0 <= r.weight <= 1.0 for r in reqs)


def test_tracker_coverage_accumulates():
    tracker = RequirementTracker(default_requirements("score"))
    assert tracker.coverage() == 0.0
    _p = make_page("product.agent_identity.capability.auth", page_type="capability")
    tracker.observe_page(_p)  # R1 需要 product/capability
    assert "R1" in tracker.met
    cov = tracker.coverage()
    assert 0.0 < cov < 1.0
    assert "R2" in tracker.missing


def test_tracker_unknown_task_falls_back_to_score():
    tracker = RequirementTracker(default_requirements("unknown-task"))
    assert tracker.coverage() == 0.0


# ── NavigationCandidate 真实深度（§10.1）─────────────────


def test_candidate_holds_real_depth():
    c = NavigationCandidate(page_id="p", depth=2, parent_page_id="parent", via_relation="related")
    assert c.depth == 2
    assert c.via_relation == "related"


async def test_navigate_stops_at_requirement_sufficient(store):
    await _single_product_wiki(store)
    nav = WikiNavigator(store)
    outcome = await nav.navigate(
        "身份认证", product_ids=["agent_identity"], task_type="score", max_depth=1
    )
    # product(0) + capability(1) 已满足 R1
    assert outcome.state.evidence_so_far >= 1


async def test_navigate_bounded_and_cycle_safe(store):
    # A→B→A 环 + 自环，不得死循环
    store.write_page(
        make_page(
            "product.agent_identity",
            product_id="agent_identity",
            relations=[
                WikiRelation(relation_type="related_to", target_page_id="product.agent_identity"),
                WikiRelation(relation_type="related_to", target_page_id="product.a.capability.aa"),
            ],
        )
    )
    store.write_page(
        make_page(
            "product.a.capability.aa",
            product_id="agent_identity",
            relations=[
                WikiRelation(relation_type="related_to", target_page_id="product.agent_identity")
            ],
        )
    )
    nav = WikiNavigator(store)
    outcome = await nav.navigate("x", product_ids=["agent_identity"], task_type="score")
    # 每个页最多访问一次
    assert len(outcome.visited) == len(set(outcome.visited))
    assert "product.a.capability.aa" in outcome.visited


# ── LLM Action 白名单 validate_action（§10.5）────────────


def _nav_state(task_type: str = "score", max_pages: int = 6, max_depth: int = 3):
    return NavigationState(
        task_type=task_type,
        query="身份认证",
        product_ids=["agent_identity"],
        max_pages=max_pages,
        max_depth=max_depth,
    )


def test_validate_action_rejects_unknown_action():
    nav = WikiNavigator.__new__(WikiNavigator)  # 不触发 __init__
    ok, reason = nav.validate_action(
        {"action": "DELETE_PAGE", "target": "x"},
        state=_nav_state(),
        frontier=[],
        visited=set(),
    )
    assert not ok
    assert reason.startswith("ACTION_NOT_ALLOWED")


def test_validate_action_rejects_invented_target():
    nav = WikiNavigator.__new__(WikiNavigator)
    frontier = [
        NavigationCandidate(page_id="product.agent_identity.capability.identity_auth", depth=1)
    ]
    ok, _ = nav.validate_action(
        {"action": "OPEN_PAGE", "target": "product.fake.page"},
        state=_nav_state(),
        frontier=frontier,
        visited=set(),
    )
    assert not ok


def test_validate_action_accepts_candidate_target():
    nav = WikiNavigator.__new__(WikiNavigator)
    frontier = [
        NavigationCandidate(page_id="product.agent_identity.capability.identity_auth", depth=1)
    ]
    ok, reason = nav.validate_action(
        {"action": "OPEN_PAGE", "target": "product.agent_identity.capability.identity_auth"},
        state=_nav_state(),
        frontier=frontier,
        visited=set(),
    )
    assert ok and reason == ""


def test_validate_action_rejects_cross_product_target():
    nav = WikiNavigator.__new__(WikiNavigator)
    ok, reason = nav.validate_action(
        {"action": "OPEN_PAGE", "target": "product.beta.capability.x"},
        state=_nav_state(),
        frontier=[],
        visited=set(),
    )
    assert not ok
    assert reason == "TARGET_NOT_IN_PRODUCTS"


def test_allowed_actions_whitelist_complete():
    for action in ALLOWED_ACTIONS:
        assert isinstance(action, str) and action.isupper()
