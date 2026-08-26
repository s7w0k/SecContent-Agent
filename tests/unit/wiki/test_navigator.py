"""PR-05 Wiki Navigator 单元测试。"""

from __future__ import annotations

from agent.wiki.contracts import WikiRelation
from agent.wiki.navigation_policy import NavigationState, budget_for
from agent.wiki.navigator import WikiNavigator
from helpers import make_page


def _build_scoring_wiki(store):
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
            relations=[
                WikiRelation(
                    relation_type="related_to",
                    target_page_id="product.agent_identity.scenario.spoofing",
                )
            ],
        )
    )
    store.write_page(
        make_page("product.agent_identity.scenario.spoofing", product_id="agent_identity")
    )


async def test_navigate_starts_at_product_and_follows_links(store):
    _build_scoring_wiki(store)
    nav = WikiNavigator(store)
    outcome = await nav.navigate(
        "Agent 身份冒用事件", product_ids=["agent_identity"], task_type="score"
    )
    visited = outcome.visited
    assert "product.agent_identity" in visited
    assert any("capability" in p for p in visited)


async def test_navigate_respects_max_pages(store):
    _build_scoring_wiki(store)
    nav = WikiNavigator(store)
    outcome = await nav.navigate(
        "Agent 身份冒用事件", product_ids=["agent_identity"], task_type="score", max_pages=1
    )
    assert len(outcome.visited) <= 1
    assert outcome.stop_reason is not None or not outcome.state.can_continue


async def test_navigate_deterministic_without_llm(store):
    _build_scoring_wiki(store)
    nav = WikiNavigator(store)
    o1 = await nav.navigate("x", task_type="score")
    o2 = await nav.navigate("x", task_type="score")
    assert o1.visited == o2.visited
    # 没有指定产品、也没有可解析索引时不应崩溃
    assert isinstance(o1.opened_pages, dict)


def test_budget_for_score():
    b = budget_for("score")
    assert b.max_pages == 6
    assert b.max_depth == 3


def test_navigation_state_limits_loop():
    state = NavigationState(max_pages=2, max_depth=3, token_budget=9999, max_tool_calls=10)
    assert state.can_continue
    state.visit("p1")
    state.visit("p2")
    assert not state.can_continue
    assert state.stop_reason == "MAX_PAGES"
