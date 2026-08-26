"""PR-03 Link Builder 单元测试。"""

from __future__ import annotations

from agent.wiki.link_builder import LinkBuilder
from helpers import make_page


def test_capability_belongs_to_product(store):
    store.write_page(make_page("product.agent_identity", product_id="agent_identity"))
    cap = make_page("product.agent_identity.capability.identity_auth", product_id="agent_identity")
    rels = LinkBuilder(store).deterministic_relations(cap)
    assert any(
        r.relation_type == "belongs_to" and r.target_page_id == "product.agent_identity"
        for r in rels
    )


def test_product_index_links_to_overview_and_capabilities(store):
    store.write_page(make_page("product.agent_identity", product_id="agent_identity"))
    store.write_page(
        make_page(
            "product.agent_identity.overview", page_type="overview", product_id="agent_identity"
        )
    )
    store.write_page(
        make_page("product.agent_identity.capability.identity_auth", product_id="agent_identity")
    )
    product = store.open_page("product.agent_identity")
    rels = LinkBuilder(store).deterministic_relations(product)
    targets = {r.target_page_id for r in rels}
    assert "product.agent_identity.overview" in targets
    assert "product.agent_identity.capability.identity_auth" in targets


def test_validate_suggestion_rejects_self_and_missing(store):
    store.write_page(make_page("product.a", product_id="a"))
    store.write_page(make_page("product.a.capability.x", product_id="a"))
    lb = LinkBuilder(store)
    assert lb.validate_suggestion("product.a", "related_to", "product.a.capability.x") is True
    assert lb.validate_suggestion("product.a", "related_to", "product.a") is False  # self loop
    assert lb.validate_suggestion("product.a", "related_to", "concept.missing") is False
    assert lb.validate_suggestion("product.a", "not_allowed", "product.a.capability.x") is False


def test_build_merges_deterministic_and_validated(store):
    store.write_page(make_page("product.a", product_id="a"))
    store.write_page(make_page("product.a.overview", page_type="overview", product_id="a"))
    product = store.open_page("product.a")
    rels = LinkBuilder(store).build(
        product,
        llm_suggestions=[
            {"type": "related_to", "target": "product.a.overview"},
            {"type": "related_to", "target": "concept.missing"},  # 应被拒绝
        ],
    )
    targets = {r.target_page_id for r in rels}
    assert "product.a.overview" in targets
    assert "concept.missing" not in targets
