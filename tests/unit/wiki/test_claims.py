"""Phase 3 / PR-10：Claim-level Provenance + Source Policy + Schema V2（G-12）。"""

from __future__ import annotations

from agent.wiki.contracts import SourceRef, WikiClaim, compute_claim_id
from agent.wiki.evidence_collector import EvidenceCollector
from agent.wiki.linter import WikiLinter
from agent.wiki.source_policy import (
    DEFAULT_SOURCE_POLICY,
    SOURCE_KIND_ALIASES,
    SourcePolicy,
)

from tests.unit.wiki.helpers import make_page, sha256_hex

# ── 稳定 Claim ID（§6.1）───────────────────────────────────


def test_claim_id_idempotent_and_normalized() -> None:
    a = compute_claim_id(product_id=" Agent ", claim_type="Capability", semantic_key=" 支持SSO ")
    b = compute_claim_id(product_id="agent", claim_type="capability", semantic_key="支持sso")
    assert a == b
    assert a.startswith("claim_")


def test_claim_id_distinguishes_semantics() -> None:
    a = compute_claim_id(product_id="p", claim_type="capability", semantic_key="supports SSO")
    b = compute_claim_id(product_id="p", claim_type="capability", semantic_key="supports MFA")
    assert a != b


def test_wiki_claim_ensure_id_fills_stable_id() -> None:
    c = WikiClaim(text="支持智能体身份认证")
    c.ensure_id(product_id="agent_identity", claim_type="capability")
    assert c.claim_id.startswith("claim_")
    # 显式 claim_id 不会被覆盖
    c2 = WikiClaim(claim_id="claim_x", text="支持智能体身份认证")
    assert c2.ensure_id(product_id="agent_identity") == "claim_x"


# ── Collector 优先消费结构化 Claim（§912 / G-12）─────────────


def _store_with_claims(store, claims) -> None:
    page = make_page(
        "capability.agent_auth",
        page_type="capability",
        product_id="agent_identity",
        source_refs=[SourceRef(source_id="s1", relative_path="overview.md", content_hash="h1")],
    )
    page.meta.claims = claims
    store.write_page(page)


def test_collector_uses_structured_claims(store) -> None:
    ref = SourceRef(source_id="s1", relative_path="overview.md", content_hash="h1")
    page = make_page(
        "capability.agent_auth",
        page_type="capability",
        product_id="agent_identity",
        source_refs=[ref],
    )
    page.meta.claims = [
        WikiClaim(
            text="支持 SSO 单点登录",
            claim_type="capability",
            source_refs=[ref],
            confidence=0.9,
        )
    ]
    collector = EvidenceCollector(store)
    items = collector.collect("sso", {page.meta.page_id: page})
    assert len(items) == 1
    item = items[0]
    assert item.claim_id.startswith("claim_")
    assert item.fact == "支持 SSO 单点登录"
    assert item.source_refs and item.source_refs[0].source_id == "s1"


def test_collector_falls_back_without_claims(store) -> None:
    page = make_page(
        "capability.agent_auth",
        page_type="capability",
        product_id="agent_identity",
        source_refs=[SourceRef(source_id="s1", relative_path="overview.md", content_hash="h1")],
    )
    assert page.meta.claims == []
    collector = EvidenceCollector(store)
    items = collector.collect("sso", {page.meta.page_id: page})
    assert items  # 命中了 Evidence & Sources 章节的行
    assert all(i.claim_id == "" for i in items)


# ── Source Policy（§6.4）────────────────────────────────────


def test_source_policy_ordering() -> None:
    p = DEFAULT_SOURCE_POLICY
    assert p.rank("official_technical_docs") < p.rank("official_marketing")
    assert p.rank("official_technical_docs") < p.rank("llm_synthesis")
    assert p.is_higher("official_datasheet", "trusted_third_party")
    assert p.best(["llm_synthesis", "official_technical_docs"]) == "official_technical_docs"
    # 未知来源归为最低优先级
    assert p.rank("unknown_kind") >= p.rank("llm_synthesis")


def test_source_policy_alias_mapping() -> None:
    p = SourcePolicy(kind_map=SOURCE_KIND_ALIASES)
    assert p.canonical_kind("release_notes") == "official_datasheet"
    assert p.rank("release_notes") == p.rank("official_datasheet")


# ── Linter：duplicate_claim_id（Phase 15）───────────────────


def test_lint_duplicate_claim_id(store) -> None:
    from agent.wiki.contracts import WikiClaim

    page = make_page(
        "product.a.capability.x",
        page_type="capability",
        product_id="a",
        source_refs=[SourceRef(source_id="s1", relative_path="a.md", content_hash=sha256_hex("t"))],
    )
    page.meta.claims = [
        WikiClaim(text="支持 SSO"),
        WikiClaim(text="支持 SSO"),
    ]
    store.write_page(page)
    result = WikiLinter(store).lint()
    assert any(e.startswith("duplicate_claim_id[") for e in result.errors)
