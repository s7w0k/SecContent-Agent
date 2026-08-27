"""Final Plan PR-A：WikiNavigator 接 LLM 决策器的集成/防越权测试（§4.9/§4.10）。"""

from __future__ import annotations

from agent.wiki.contracts import WikiRelation
from agent.wiki.navigation_decider import NavigationAction
from agent.wiki.navigator import WikiNavigator
from helpers import make_page


class StubDecider:
    """可控的假决策器：按队列返回动作；耗尽后 STOP_INSUFFICIENT。"""

    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    async def decide(self, context):
        self.calls += 1
        if not self.actions:
            return NavigationAction(action="STOP_INSUFFICIENT")
        return self.actions.pop(0)


def _chain_wiki(store):
    store.write_page(
        make_page(
            "product.agent",
            product_id="agent",
            relations=[
                WikiRelation(
                    relation_type="related_to",
                    target_page_id="product.agent.capability.auth",
                )
            ],
        )
    )
    store.write_page(
        make_page(
            "product.agent.capability.auth",
            product_id="agent",
            relations=[
                WikiRelation(
                    relation_type="related_to",
                    target_page_id="product.agent.scenario.spoofing",
                )
            ],
        )
    )
    store.write_page(make_page("product.agent.scenario.spoofing", product_id="agent"))


async def test_llm_drives_next_page_selection(store):
    """LLM 依次选择 product → capability，结果反映 LLM 决策而非纯 BFS。"""
    _chain_wiki(store)
    decider = StubDecider(
        [
            NavigationAction(action="OPEN_PAGE", target="product.agent"),
            NavigationAction(action="OPEN_PAGE", target="product.agent.capability.auth"),
        ]
    )
    nav = WikiNavigator(store, llm=object(), decider=decider)
    outcome = await nav.navigate("认证", product_ids=["agent"], task_type="score")
    assert decider.calls >= 2
    assert "product.agent.capability.auth" in outcome.visited
    # Trace 出现 LLM 决策事件（§4.10 验收）
    llm_events = [t for t in outcome.trace if t["event"] == "wiki.llm_decision"]
    assert any(t.get("status") == "ACCEPTED" for t in llm_events)


async def test_llm_cannot_invent_page_id(store):
    """LLM 提出不存在的 page_id → reject，回退 deterministic，绝不打开越权页。"""
    _chain_wiki(store)
    decider = StubDecider(
        [
            NavigationAction(action="OPEN_PAGE", target="product.fake.unknown"),
            NavigationAction(action="OPEN_PAGE", target="product.agent"),
        ]
    )
    nav = WikiNavigator(store, llm=object(), decider=decider)
    outcome = await nav.navigate("认证", product_ids=["agent"], task_type="score")
    assert "product.fake.unknown" not in outcome.visited
    invalid = [t for t in outcome.trace if t.get("status") == "INVALID"]
    assert invalid


async def test_llm_cannot_reopen_visited_page(store):
    """重复打开已访问页 → TARGET_ALREADY_VISITED，被 Harness 拒绝。"""
    _chain_wiki(store)
    decider = StubDecider(
        [
            NavigationAction(action="OPEN_PAGE", target="product.agent"),
            NavigationAction(action="OPEN_PAGE", target="product.agent"),
        ]
    )
    nav = WikiNavigator(store, llm=object(), decider=decider)
    outcome = await nav.navigate("认证", product_ids=["agent"], task_type="score")
    assert "product.agent" in outcome.visited
    assert "product.agent.capability.auth" in outcome.visited  # 第二条被挡后 fallback 继续导航


async def test_llm_repeated_action_falls_back(store):
    """相同 action+target 连续 ≥2 → 回退 deterministic（§4.8）。"""
    _chain_wiki(store)
    decider = StubDecider(
        [
            NavigationAction(action="OPEN_PAGE", target="product.agent"),
            NavigationAction(action="OPEN_PAGE", target="product.agent.capability.auth"),
            NavigationAction(action="OPEN_PAGE", target="product.agent.capability.auth"),
        ]
    )
    nav = WikiNavigator(store, llm=object(), decider=decider)
    outcome = await nav.navigate("认证", product_ids=["agent"], task_type="score")
    # 不崩溃且完成导航
    assert outcome.stop_reason is not None


async def test_llm_stop_sufficient_rejected_when_requirements_missing(store):
    """需求未补齐时 LLM 请求 STOP_SUFFICIENT → 拒绝（不得谎称充分）。"""
    _chain_wiki(store)
    decider = StubDecider([NavigationAction(action="STOP_SUFFICIENT")])
    nav = WikiNavigator(store, llm=object(), decider=decider)
    outcome = await nav.navigate("认证", product_ids=["agent"], task_type="score")
    rejected = [t for t in outcome.trace if t.get("status") == "INVALID"]
    assert rejected  # STOP 被拒；导航回退继续
    assert "product.agent" in outcome.visited  # deterministic 继续打开起始页
    assert outcome.state.stop_reason != "SUFFICIENT"


async def test_llm_disabled_uses_deterministic_only(store):
    """WIKI_NAVIGATOR_LLM_ENABLED=false → 完全 deterministic，无 LLM 决策事件。"""
    _chain_wiki(store)
    nav = WikiNavigator(store)  # llm=None → llm_enabled=False
    outcome = await nav.navigate("认证", product_ids=["agent"], task_type="score")
    assert nav.llm_enabled is False
    assert "product.agent.capability.auth" in outcome.visited
    assert all(t["event"] != "wiki.llm_decision" for t in outcome.trace)


async def test_llm_timeout_falls_back_without_breaking_request(store):
    """LLM 决策抛错 → 导航仍能完成（§4.10 验收：LLM 故障请求仍可完成）。"""
    _chain_wiki(store)

    class FailingDecider:
        async def decide(self, context):
            raise RuntimeError("timeout")

    nav = WikiNavigator(store, llm=object(), decider=FailingDecider())
    outcome = await nav.navigate("认证", product_ids=["agent"], task_type="score")
    assert "product.agent" in outcome.visited
    assert outcome.state.llm_failure_count >= 1
    failed = [t for t in outcome.trace if t.get("status") == "FAILED"]
    assert failed
